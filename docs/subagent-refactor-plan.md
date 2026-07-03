# SubAgent 模块重构方案

> **状态**: Phase 1-6 已实现 ✅ | 测试待补充 ⏳
>
> 参考 DeerFlow `deerflow/subagents/` 实现，对当前 `harness/agents/` 下
> `subagent.py`、`subagent_manager.py` 及相关模块进行全面重构。

---

## 一、当前架构 vs 目标架构

### 当前 (v1)

```
Lead Agent (同一 event loop)
  └─ task 工具 → SubagentManager.execute()
       └─ SubAgent.execute()
            └─ self._graph.ainvoke(state, RunnableConfig())  ← inline, 同一 task 树
            └─ 20 个中间件全部运行
            └─ 无超时控制, 无取消隔离
```

### 目标 (v2, 对齐 DeerFlow)

```
Lead Agent (父 event loop)
  └─ task 工具 → SubagentManager.execute()
       └─ SubAgentExecutor._aexecute(task, result_holder)
            ├─ 运行在独立 daemon 线程的持久化 event loop 中
            ├─ 精简中间件链 (7-9 个)
            ├─ astream(stream_mode="values") — 支持流式 + 协作式取消
            ├─ SubagentTokenCollector callback — 轻量级 token 追踪
            └─ Future.result(timeout=config.timeout_seconds)
```

---

## 二、核心改动清单

### 2.1 执行隔离（优先级：🔴 高）

**问题**：SubAgent 的 `ainvoke()` 运行在父请求的 asyncio task 树中，用户断开连接
时 `CancelledError` 会级联取消 SubAgent，Langfuse 中产生 error。

**目标**：将 SubAgent 执行移到独立的 daemon 线程 + 持久化 event loop 中。

**技术要点**：

```python
# 1. 持久化独立 event loop（模块级单例）
_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
_isolated_subagent_loop_lock = threading.Lock()

def _get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    """返回持久化 event loop, 不存在则创建 daemon 线程并启动."""
    global _isolated_subagent_loop, _isolated_subagent_loop_thread
    with _isolated_subagent_loop_lock:
        if _isolated_subagent_loop is None or _isolated_subagent_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
                name="subagent-loop", daemon=True,
            )
            thread.start()
            _isolated_subagent_loop = loop
            _isolated_subagent_loop_thread = thread
        return _isolated_subagent_loop

# 2. atexit 清理
import atexit
def _shutdown_isolated_subagent_loop():
    global _isolated_subagent_loop
    with _isolated_subagent_loop_lock:
        loop = _isolated_subagent_loop
        _isolated_subagent_loop = None
    if loop and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
atexit.register(_shutdown_isolated_subagent_loop)

# 3. ContextVar 传递 — 保留 thread_id 等上下文
from contextvars import copy_context

def _submit_to_isolated_loop(coro_factory):
    context = copy_context()
    return context.run(
        lambda: asyncio.run_coroutine_threadsafe(
            coro_factory(), _get_isolated_subagent_loop()
        )
    )

# 4. 协作式取消
class SubagentResult:
    cancel_event: threading.Event = field(default_factory=threading.Event)

# 在 astream 迭代中检查:
async for chunk in agent.astream(...):
    if result.cancel_event.is_set():
        result.try_set_terminal(SubagentStatus.CANCELLED, error="Cancelled by user")
        return result
```

**⚠️ 边界注意**：
- `copy_context()` 必须复制，否则 ContextVar（如 `thread_id`）在子线程中丢失
- daemon 线程的 event loop 在 atexit 中必须正确关闭，否则进程退出时可能卡住
- `asyncio.run_coroutine_threadsafe()` 返回的 `Future` 支持 `timeout` 超时控制

---

### 2.2 中间件精简（优先级：🔴 高）

**问题**：当前 SubAgent 与 Lead Agent 共用全部 20 个中间件，存在性能浪费和
副作用风险（如 `MemoryMiddleware` 将 SubAgent 对话写入用户记忆）。

**目标**：为 SubAgent 构建专用中间件链，只保留必需的 7-9 个。

**必须保留（7 个）**：

| # | 中间件 | 作用 | 备注 |
|---|--------|------|------|
| 1 | `ThreadDataMiddleware` | 沙箱目录初始化 | 需要传递 `thread_id` / `user_id` |
| 2 | `SandboxMiddleware` | 沙箱生命周期 | 继承父 sandbox_id |
| 3 | `DanglingToolCallMiddleware` | 修复中断导致的悬空 tool_call | — |
| 4 | `LLMErrorHandlingMiddleware` | LLM 调用异常 → 友好错误 | 含重试逻辑 |
| 5 | `ToolErrorHandlingMiddleware` | 工具异常 → error ToolMessage | 不中断执行 |
| 6 | `SandboxAuditMiddleware` | 沙箱操作安全审计 | — |
| 7 | `SafetyFinishReasonMiddleware` | 安全终止检测 | 如果配置启用 |

**条件保留（2 个）**：

| # | 中间件 | 条件 |
|---|--------|------|
| 8 | `ViewImageMiddleware` | 仅 model 支持 vision |
| 9 | `GuardrailMiddleware` | 仅 guardrail 配置启用 |

**必须排除（11 个）及原因**：

| 中间件 | 排除原因 |
|--------|----------|
| `UploadsMiddleware` | SubAgent 不处理新文件上传 |
| `DynamicContextMiddleware` | 注入内容可能与 SubAgent system_prompt 冲突 |
| `SummarizationMiddleware` | SubAgent 用 `max_turns` 限制，不需要摘要 |
| `TodoMiddleware` | SubAgent 不支持 plan_mode |
| `TokenUsageMiddleware` | 替换为 `SubagentTokenCollector` callback |
| `TitleMiddleware` | SubAgent 不生成 thread 标题 |
| `MemoryMiddleware` | **严重副作用** — SubAgent 对话不应写入用户记忆 |
| `DeferredToolFilterMiddleware` | SubAgent 没有 tool_search 机制 |
| `SubagentLimitMiddleware` | SubAgent 的 `task` 已在 disallowed_tools 中 |
| `LoopDetectionMiddleware` | `max_turns` 已有循环保护 |
| `ClarificationMiddleware` | `ask_clarification` 已在 disallowed_tools 中 |

**实现**：

```python
def build_subagent_middlewares(
    *,
    config_manager: ConfigManager | None = None,
    vision_enabled: bool = False,
    guardrail_enabled: bool = False,
) -> list[AgentMiddleware]:
    """构建 SubAgent 专用中间件链（精简版, 7-9 个）."""
    middlewares: list[AgentMiddleware] = [
        ThreadDataMiddleware(),
        SandboxMiddleware(),
        DanglingToolCallMiddleware(),
        LLMErrorHandlingMiddleware(),
        SandboxAuditMiddleware(),
        ToolErrorHandlingMiddleware({"max_retries": 3}),
    ]
    if guardrail_enabled:
        middlewares.insert(5, GuardrailMiddleware())  # 在 SandboxAudit 之前
    if vision_enabled:
        middlewares.append(ViewImageMiddleware())
    middlewares.append(SafetyFinishReasonMiddleware())
    return middlewares
```

---

### 2.3 模型解析（优先级：🔴 高，已在 #2 部分修复）

**当前代码（已在上一步修复）**：

```python
# subagent_manager.py
if model_name == "inherit":
    model_name = parent_model or None  # None → _init_llm 内部用 config.default_model
```

**进一步对齐 DeerFlow**：支持 config.yaml 中对特定 SubAgent 覆盖 model：

```yaml
# config.yaml
subagents:
  timeout_seconds: 900
  agents:
    researcher:
      model: gpt-4o-mini       # ← 研究员用更便宜的模型
      max_turns: 40
    coder:
      model: gpt-4o            # ← 编码员用更强的模型
```

```python
def resolve_subagent_model(
    config: SubAgentConfig,
    parent_model: str | None,
    *,
    app_config=None,
) -> str:
    """解析 SubAgent 的有效模型名."""
    # 1. config.model 显式指定 → 直接使用
    if config.model != "inherit":
        return config.model
    # 2. parent_model 传递 → 继承
    if parent_model is not None:
        return parent_model
    # 3. config.yaml agents.{name}.model 覆盖
    if app_config and (override := app_config.get_subagent_model(config.name)):
        return override
    # 4. 全局 default_model
    return app_config.default_model if app_config else "gpt-4o"
```

---

### 2.4 State 继承（优先级：🟡 中）

**当前问题**：`parent_state` 只传递 `thread_id` / `user_id` 两个字段。而 DeerFlow
传递完整的 `sandbox_state` + `thread_data` 对象。

**需要额外传递的字段**：

| 字段 | 作用 | 传递方式 |
|------|------|----------|
| `sandbox_id` / `sandbox` | SubAgent 复用同一个沙箱 | 从 parent_state 提取 |
| `thread_data` | 工作区路径映射（workspace/uploads/outputs） | 从 parent_state 提取 |
| `thread_id` | 沙箱 provider 路由 | 已有 ✅ |
| `user_id` | 用户隔离 | 已有 ✅ |

**实现**：

```python
# SubAgentExecutor._build_initial_state()
state: dict[str, Any] = {
    "messages": messages,
}
# 继承 sandbox — 避免创建新沙箱
if parent_sandbox := parent_state.get("sandbox"):
    state["sandbox"] = parent_sandbox
# 继承 thread_data — 复用工作区路径
if parent_thread_data := parent_state.get("thread_data"):
    state["thread_data"] = parent_thread_data
```

**⚠️ 边界注意**：
- 如果不传 `sandbox`，`SandboxMiddleware` 会为 SubAgent 创建**新的**沙箱，导致
  路径隔离问题（文件写到了不同位置）
- 如果不传 `thread_data`，`ThreadDataMiddleware` 会重新创建目录，浪费磁盘 I/O

---

### 2.5 执行模式升级：ainvoke → astream（优先级：🟡 中）

**当前**：`self._graph.ainvoke(state, RunnableConfig())` — 一次性等待全部结果。

**目标**：`agent.astream(state, stream_mode="values")` — 流式迭代，支持：
1. 协作式取消（每次迭代边界检查 `cancel_event`）
2. 逐条收集 AIMessage（用于 token 统计和中间结果缓存）
3. 超时控制

```python
async def _aexecute(self, task: str, result_holder: SubagentResult) -> SubagentResult:
    collector = SubagentTokenCollector(caller=f"subagent:{self.config.name}")
    run_config: RunnableConfig = {
        "recursion_limit": self.config.max_turns,
        "callbacks": [collector],
    }
    if self.thread_id:
        run_config["configurable"] = {"thread_id": self.thread_id}

    final_state = None
    async for chunk in agent.astream(
        state, config=run_config, stream_mode="values"
    ):
        # 协作式取消检查
        if result_holder.cancel_event.is_set():
            result_holder.try_set_terminal(
                SubagentStatus.CANCELLED,
                error="Cancelled by user",
                token_usage_records=collector.snapshot_records(),
            )
            return result_holder

        final_state = chunk
        # 收集 AIMessage
        messages = chunk.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage):
            result_holder.ai_messages.append(messages[-1].model_dump())

    # 提取最终结果
    final_result = self._extract_final_message(final_state)
    result_holder.try_set_terminal(
        SubagentStatus.COMPLETED,
        result=final_result,
        token_usage_records=collector.snapshot_records(),
    )
    return result_holder
```

---

### 2.6 Token 追踪（优先级：🟢 低）

**DeerFlow 方案**：`SubagentTokenCollector(BaseCallbackHandler)` — 轻量 callback，
在 `on_llm_end()` 中按 `run_id` 去重收集 usage。

```python
class SubagentTokenCollector(BaseCallbackHandler):
    """收集 SubAgent 内部的 LLM token 用量."""

    def __init__(self, caller: str):
        super().__init__()
        self.caller = caller
        self._records: list[dict] = []
        self._counted_run_ids: set[str] = set()

    def on_llm_end(self, response, *, run_id, **kwargs):
        rid = str(run_id)
        if rid in self._counted_run_ids:
            return
        for generation in response.generations:
            for gen in generation:
                usage = getattr(gen.message, "usage_metadata", None)
                if usage:
                    self._counted_run_ids.add(rid)
                    self._records.append({
                        "source_run_id": rid,
                        "caller": self.caller,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    })

    def snapshot_records(self) -> list[dict]:
        return list(self._records)
```

**优势**：不依赖 middleware，不修改 state，纯 callback 模式。

---

### 2.7 System Prompt 合并（优先级：🟢 低）

**问题**：部分 LLM API（如 Anthropic Claude）不允许**多条 SystemMessage**，
会报错 `System message must be at the beginning`。

**DeerFlow 做法**：将 system_prompt + skill 内容合并为**一条** SystemMessage。

```python
# 合并而非分开发送
system_parts = []
if self.config.system_prompt:
    system_parts.append(self.config.system_prompt)
for skill_msg in skill_messages:
    system_parts.append(skill_msg.content)

messages = []
if system_parts:
    messages.append(SystemMessage(content="\n\n".join(system_parts)))
messages.append(HumanMessage(content=task))
```

**⚠️ 注意**：当前项目的 `DynamicContextMiddleware` 可能在 SubAgent 中额外注入
`<system-reminder>`，这会创建第二条 SystemMessage。精简中间件后此问题自然消失。

---

### 2.8 SubagentResult 增强（优先级：🟢 低）

**当前**：
```python
class SubAgentResult(BaseModel):
    status: str          # "success" | "error" | "max_iterations_reached"
    output: str
    iterations: int
```

**目标（对齐 DeerFlow）**：
```python
@dataclass
class SubagentResult:
    task_id: str
    trace_id: str
    status: SubagentStatus   # PENDING | RUNNING | COMPLETED | FAILED | CANCELLED | TIMED_OUT
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ai_messages: list[dict] = field(default_factory=list)
    token_usage_records: list[dict] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _state_lock: threading.Lock = field(default_factory=threading.Lock)

    def try_set_terminal(self, status, *, result=None, error=None, ...):
        """线程安全的一次性终止状态设置 — 先到先得."""
        with self._state_lock:
            if self.status.is_terminal:
                return False
            self.status = status
            ...
            return True
```

**新字段的意义**：

| 字段 | 意义 |
|------|------|
| `task_id` | 支持后台执行 + 状态轮询 |
| `trace_id` | 连接父 agent 和 subagent 的 tracing |
| `cancel_event` | 协作式取消的信号量 |
| `ai_messages` | 收集 SubAgent 完整推理过程 |
| `token_usage_records` | SubAgent 独立 token 统计 |
| `_state_lock` | 防止超时和正常完成并发写 |

---

### 2.9 并发控制与超时（优先级：🟡 中）

**当前**：`asyncio.Semaphore(max_concurrent=2~4)` + `max_turns` 限制。

**缺失**：没有 wall-clock 超时。如果 SubAgent 的工具调用卡住（如网络超时），
`max_turns` 无法触发。

**目标**：
```python
# 1. 线程池调度（不阻塞主 event loop）
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-")

# 2. 超时控制
try:
    future = _submit_to_isolated_loop(coro_factory)
    return future.result(timeout=self.config.timeout_seconds)  # 默认 900s
except FuturesTimeoutError:
    result_holder.cancel_event.set()   # 协作式取消
    result_holder.try_set_terminal(SubagentStatus.TIMED_OUT, error="...")
    future.cancel()
```

**配置项**：
```yaml
# config.yaml
subagents:
  timeout_seconds: 900    # 全局默认
  max_concurrent: 3       # 并发数
  agents:
    coder:
      timeout_seconds: 300  # 编码子 agent 超时更短
```

---

## 三、完整的类结构调整

### 重构后文件结构

```
harness/agents/
├── subagent.py              # SubAgent 配置类（精简, 保留）
├── subagent_manager.py      # SubagentManager 生命周期管理（修改）
├── subagent_executor.py     # [新] SubAgentExecutor — 隔离执行引擎
├── subagent_middleware.py   # [新] build_subagent_middlewares()
├── subagent_token.py        # [新] SubagentTokenCollector callback
├── presets.py               # 预设配置（已在上一步更新工具列表）
├── lead_agent.py            # Lead Agent（不变）
└── ...
```

### SubAgentExecutor 核心方法

```
SubAgentExecutor
├── __init__(config, tools, parent_state, thread_id, trace_id)
├── _create_agent(tools) → Runnable
├── _build_initial_state(task) → (dict, filtered_tools)
├── _aexecute(task, result_holder) → SubagentResult     # async, 核心循环
├── _execute_in_isolated_loop(task, result_holder)       # sync, 桥接到独立 loop
├── execute(task) → SubagentResult                       # 同步入口
└── execute_async(task, task_id) → task_id               # 后台异步执行
```

---

## 四、实施顺序建议

| 阶段 | 改动 | 风险 | 收益 |
|------|------|------|------|
| **Phase 1** | 中间件精简 + 模型解析修复 | 低 | 消除 `MemoryMiddleware` 副作用, 修复 `gpt-4o` 硬编码 |
| **Phase 2** | State 继承增强（传递 sandbox + thread_data） | 低 | 避免 SubAgent 创建新沙箱 |
| **Phase 3** | SubagentResult 增强 + `try_set_terminal` | 低 | 为 Phase 4 做数据结构准备 |
| **Phase 4** | 执行隔离（独立 event loop + daemon 线程） | **高** | 解决 CancelledError, 提升稳定性 |
| **Phase 5** | `ainvoke` → `astream` + TokenCollector | 中 | 流式输出 + 协作式取消 + token 追踪 |
| **Phase 6** | 超时控制 + 线程池调度 | 中 | 防止永久挂起 |

**建议**：Phase 1-3 可以一起做（低风险），Phase 4-6 依次推进（各有独立验证点）。

---

## 五、测试清单

### 单元测试

- [ ] `SubagentTokenCollector` — 去重 + usage 提取正确
- [ ] `build_subagent_middlewares()` — 数量正确, 排除项正确
- [ ] `resolve_subagent_model()` — inherit / parent / override / default 四条路径
- [ ] `SubagentResult.try_set_terminal()` — 并发写入只取第一个
- [ ] `_filter_tools()` — allowlist + denylist 逻辑

### 集成测试

- [ ] SubAgent 在独立线程中执行 → 父请求取消后 SubAgent 继续完成
- [ ] `cancel_event.set()` → SubAgent 在下一迭代边界停止
- [ ] `timeout_seconds` 超时 → `SubagentStatus.TIMED_OUT`
- [ ] `thread_data` 传递 → SubAgent 写入文件到正确的工作区
- [ ] `sandbox` 传递 → SubAgent 复用父沙箱, 不创建新沙箱
- [ ] 中间件排除验证 → `MemoryMiddleware` 未运行, 无副作用写入

### 回归测试

- [ ] `task` 工具正常委派任务 → 正确的 SubAgent 类型被创建和执行
- [ ] `create_subagent` 工具正常创建 SubAgent
- [ ] Lead Agent 正常接收 SubAgent 返回结果并合成答案
- [ ] 并发 3 个 SubAgent 同时执行 → Semaphore 门控正确

---

## 六、关键边界规则

1. **禁止嵌套** — `task` 工具必须在 SubAgent 的 `disallowed_tools` 中
2. **禁止反问** — `ask_clarification` 必须在 SubAgent 的 `disallowed_tools` 中
3. **沙箱复用** — 必须传递 `sandbox` / `sandbox_id`, 避免每个 SubAgent 创建新沙箱
4. **路径一致性** — `thread_data` 中的 workspace/uploads/outputs 路径映射必须传递
5. **ContextVar 传递** — 跨线程时必须 `copy_context()`, 否则 ContextVar 丢失
6. **单条 SystemMessage** — system_prompt 合并为一条, 避免多 SystemMessage API 报错
7. **协作式取消** — 不使用 `Future.cancel()` 强制终止, 改为 `threading.Event` + 迭代检查
8. **atexit 清理** — daemon 线程的 event loop 必须在 atexit 中 stop + close
9. **模型配置分层** — 优先级: config.model > parent_model > config.yaml override > default_model
10. **最大 turns 小于 Lead Agent** — SubAgent 的 max_turns 应设置在 30-60, 因为子任务应是聚焦的
