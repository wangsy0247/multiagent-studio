# Subagent 使用原理深度解析：DeerFlow 与 MultiAgent-Studio

> 本文基于对 `deer-flow` 与 `multiagent-studio` 两个开源 Agent 框架的源码阅读，梳理 subagent 的完整运行流程、涉及的核心技术点以及需要关注的边界条件，并配合代码片段举例说明。

---

## 1. 什么是 Subagent？为什么需要它？

Subagent（子代理）是主代理（Lead Agent）把一部分复杂任务**委托**给另一个独立代理去执行的机制。它的核心价值在于：

- **上下文隔离**：子代理不会污染主对话历史。
- **专业分工**：不同 subagent 可配置不同 system prompt、工具集、模型。
- **并行执行**：多个 subagent 可同时运行，提升效率。
- **安全控制**：通过 allowlist/denylist 限制子代理可调用的工具，避免危险操作。

两个项目都实现了 subagent，但设计哲学略有不同：

| 维度 | DeerFlow | MultiAgent-Studio |
|------|----------|-------------------|
| 设计定位 | 内置 + 用户自定义 subagent | 预设（preset）+ 运行时动态创建 |
| 执行模型 | 后台任务 + 父代理轮询 | 同步/异步执行 + SSE 实时推送 |
| 通信方式 | `ai_messages` 列表轮询 | `asyncio.Queue` 实时流 |
| 状态枚举 | `PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/TIMED_OUT` | `RUNNING/SUCCESS/ERROR/MAX_ITERATIONS_REACHED/CANCELLED/TIMED_OUT` |

---

## 2. Subagent 模块在哪里？

### 2.1 DeerFlow

核心代码位于 `deer-flow/backend/packages/harness/deerflow/subagents/`：

| 文件 | 作用 |
|------|------|
| `executor.py` | `SubagentExecutor`、`SubagentResult`、`SubagentStatus`，以及隔离事件循环、后台任务注册表 |
| `registry.py` | subagent 配置解析、内置 + 自定义 agent 查找 |
| `config.py` | `SubagentConfig` 与模型名解析 |
| `token_collector.py` | 子代理 token 用量收集 |
| `builtins/general_purpose.py` | 内置 `general-purpose` subagent |
| `builtins/bash_agent.py` | 内置 `bash` subagent |
| `tools/builtins/task_tool.py` | Lead Agent 调用的 `task` 工具 |
| `agents/middlewares/subagent_limit_middleware.py` | 每轮 `task` 调用数量上限控制 |

### 2.2 MultiAgent-Studio

核心代码位于 `multiagent-studio/harness/agents/`：

| 文件 | 作用 |
|------|------|
| `subagent_executor.py` | 隔离执行引擎、持久化事件循环、实时消息流队列 |
| `subagent_manager.py` | subagent 生命周期管理 + 并发信号量 |
| `subagent_middleware.py` | 子代理专用精简中间件链 |
| `subagent_token.py` | token 用量回调 |
| `presets.py` | 内置 preset（researcher / coder / analyst / writer / reviewer） |
| `tools/builtins/lead_tools.py` | `create_subagent` 与 `task` 工具 |
| `middleware/subagent_limit.py` | 每轮 `task` 调用上限 |

---

## 3. Subagent 的完整生命周期

虽然两个项目实现细节不同，但生命周期可抽象为四个阶段：**创建 → 执行 → 通信 → 终止**。

### 3.1 创建（Creation）

#### DeerFlow：通过 `task` 工具触发

```python
@tool("task", parse_docstring=True)
async def task_tool(
    runtime: Runtime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    # 1. 根据 subagent_type 解析配置
    config = get_subagent_config(subagent_type, app_config=runtime_app_config)
    # 2. 从 runtime 提取父上下文
    #    - sandbox_state, thread_data_state, thread_id, parent model 等
    # 3. 加载工具，但 subagent_enabled=False 防止递归嵌套
    tools = get_available_tools(
        model_name=effective_model,
        groups=parent_tool_groups,
        subagent_enabled=False,
    )
    # 4. 构造执行器
    executor = SubagentExecutor(
        config=config,
        tools=tools,
        parent_model=parent_model,
        sandbox_state=sandbox_state,
        thread_data=thread_data,
        thread_id=thread_id,
        trace_id=trace_id,
        app_config=resolved_app_config,
    )
```

配置解析优先级：
1. 内置 subagent（`general-purpose`、`bash`）
2. `config.yaml` 中 `subagents.custom_agents` 的自定义 agent
3. `config.yaml` 中 `subagents.agents.<name>` 的覆盖配置

#### MultiAgent-Studio：可预设或运行时创建

```python
# 方式一：预设（presets.py）
RESEARCHER_CONFIG = SubAgentConfig(
    name="researcher",
    system_prompt="...",
    tools=["web_search", "arxiv_search", "web_fetch"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=20,
)

# 方式二：运行时通过 create_subagent 工具创建
async def create_subagent(
    name: str,
    agent_type: Literal["researcher", "coder", "analyst", "writer", "reviewer"],
    description: str = "",
    custom_system_prompt: str = "",
) -> str:
    config = build_subagent_config(name, agent_type, description, custom_system_prompt)
    await manager.create(config)
```

创建时会解析模型（`inherit` 表示继承父模型），并过滤工具集。

### 3.2 执行（Execution）

两个项目都使用 **LangGraph** 构建子代理图，并通过 `agent.astream(..., stream_mode="values")` 运行。

#### DeerFlow 的执行入口

```python
class SubagentExecutor:
    def execute(self, task: str, result_holder=None) -> SubagentResult:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # 父代理已经在运行的事件循环中：使用独立的持久化事件循环
            return self._execute_in_isolated_loop(task, result_holder)
        # 没有运行中的循环：简单 asyncio.run
        return asyncio.run(self._aexecute(task, result_holder))

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        # 后台执行，被 task_tool 调用
        ...
```

#### MultiAgent-Studio 的执行入口

```python
class SubagentExecutor:
    def execute(self, task: str) -> SubAgentResult:
        result = SubAgentResult(
            task_id=str(uuid.uuid4())[:8],
            status=SubagentStatus.RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._current_result = result
        try:
            running_loop = asyncio.get_running_loop()
            if running_loop is not None and running_loop.is_running():
                return self._execute_in_isolated_loop(task, result)
            return asyncio.run(self._aexecute(task, result))
        except Exception as exc:
            result.try_set_terminal(SubagentStatus.ERROR, error=str(exc), output=str(exc))
            return result
```

两者都判断是否在运行的事件循环中：
- 如果不在运行循环中：直接用 `asyncio.run()`。
- 如果在运行循环中（比如 Lead Agent 本身就是 async LangGraph）：将协程提交到一个**持久化的独立事件循环**（daemon thread）中执行，避免 `asyncio.run()` 嵌套错误和客户端绑定问题。

#### 共享的隔离事件循环实现

```python
_isolated_loop: asyncio.AbstractEventLoop | None = None
_isolated_loop_thread: threading.Thread | None = None
_isolated_loop_lock = threading.Lock()

def _get_isolated_loop() -> asyncio.AbstractEventLoop:
    global _isolated_loop, _isolated_loop_thread
    with _isolated_loop_lock:
        thread_alive = _isolated_loop_thread is not None and _isolated_loop_thread.is_alive()
        loop_usable = (
            _isolated_loop is not None
            and not _isolated_loop.is_closed()
            and _isolated_loop.is_running()
            and thread_alive
        )
        if not loop_usable:
            loop = asyncio.new_event_loop()
            started = threading.Event()
            thread = threading.Thread(
                target=_run_isolated_loop,
                args=(loop, started),
                name="subagent-isolated-loop",
                daemon=True,
            )
            thread.start()
            if not started.wait(timeout=5):
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
                loop.close()
                raise RuntimeError("Timed out starting isolated subagent event loop")
            _isolated_loop = loop
            _isolated_loop_thread = thread
        return _isolated_loop
```

> 这是一个**单例 daemon 线程**，所有需要在运行循环中执行的 subagent 都共享它，避免频繁创建/关闭事件循环带来的 async 客户端（如 `httpx`）绑定问题。

#### 核心 ReAct 循环

```python
async def _aexecute(self, task, result_holder):
    state = self._build_initial_state(task)
    agent = self._create_agent(self._base_tools)
    collector = SubagentTokenCollector(caller=f"subagent:{self.config.name}")

    run_config = {
        "recursion_limit": self.config.max_turns,
        "callbacks": [collector],
        "tags": [f"subagent:{self.config.name}"],
    }
    if self.thread_id:
        run_config["configurable"] = {"thread_id": self.thread_id}

    async for chunk in agent.astream(state, config=run_config, stream_mode="values"):
        if result_holder.cancel_event.is_set():
            result_holder.try_set_terminal(SubagentStatus.CANCELLED, ...)
            return result_holder
        # 处理新消息...
```

关键参数：
- `recursion_limit`：对应 `max_turns`，防止无限循环。
- `callbacks`：注入 token 收集器。
- `tags`：标记为 subagent 事件，便于父级过滤。
- `configurable.thread_id`：继承父线程，保证沙箱/路径上下文一致。

### 3.3 通信（Communication）

#### DeerFlow：后台轮询模式

`task_tool` 调用 `execute_async()` 后，subagent 在后台线程运行。父代理每 5 秒轮询一次结果：

```python
poll_count = 0
last_message_count = 0
max_poll_count = (config.timeout_seconds + 60) // 5

while True:
    result = get_background_task_result(task_id)
    ai_messages = result.ai_messages or []
    current_message_count = len(ai_messages)

    # 流式推送中间 AI 消息
    if current_message_count > last_message_count:
        for i in range(last_message_count, current_message_count):
            writer({
                "type": "task_running",
                "task_id": task_id,
                "message": ai_messages[i],
            })
        last_message_count = current_message_count

    # 判断终止状态
    if result.status == SubagentStatus.COMPLETED: ...
    elif result.status == SubagentStatus.FAILED: ...
    elif result.status == SubagentStatus.CANCELLED: ...
    elif result.status == SubagentStatus.TIMED_OUT: ...

    await asyncio.sleep(5)
    poll_count += 1
    if poll_count > max_poll_count:
        # 安全网超时
        ...
```

#### MultiAgent-Studio：实时队列模式

子代理在隔离循环中将消息推送到以 subagent 名称命名的 `asyncio.Queue`：

```python
_subagent_streams: dict[str, "asyncio.Queue[dict[str, Any]]"] = {}
_subagent_streams_lock = threading.Lock()

def get_subagent_stream(name: str) -> "asyncio.Queue[dict[str, Any]]":
    with _subagent_streams_lock:
        if name not in _subagent_streams:
            _subagent_streams[name] = asyncio.Queue()
        return _subagent_streams[name]
```

主服务 `HarnessService.execute()` 会并发消费这些队列，并向前端发送 `subagent_thinking`、`subagent_tool_call`、`subagent_tool_result`、`subagent_progress` 等 SSE 事件。父级 graph 的 `astream_events` 会通过 `tags` 过滤掉 subagent 内部事件：

```python
_event_tags = event.get("tags", []) or []
if any(t.startswith("subagent:") for t in _event_tags):
    continue
```

### 3.4 终止（Termination）

两个项目都定义了类似的终止状态：

```python
# DeerFlow
class SubagentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

# MultiAgent-Studio
class SubagentStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
```

关键设计：**单次写入的终端状态转换**。

```python
def try_set_terminal(self, status, *, result=None, error=None, ...) -> bool:
    with self._state_lock:
        if self.status.is_terminal:
            return False  # 第一个终止状态获胜
        self.result = result
        self.error = error
        self.ai_messages = ai_messages
        self.completed_at = completed_at or datetime.now()
        self.status = status
        return True
```

这个锁非常重要：超时和完成可能同时发生，必须保证不会互相覆盖。

---

## 4. 核心技术点拆解

### 4.1 并发模型

| 层级 | 实现 |
|------|------|
| 每轮调用上限 | `SubagentLimitMiddleware` 截断每轮模型响应中超出的 `task` tool call |
| 并发执行上限 | `asyncio.Semaphore` 或 `ThreadPoolExecutor(max_workers=3)` |
| 隔离事件循环 | 单例 daemon thread 运行持久化 `asyncio` loop |
| 线程安全 | 对 `_background_tasks` 和 `_subagent_streams` 加锁；`SubagentResult` 状态转换加锁 |

最大并发数默认值都是 3，并被限制在 `[2, 4]` 区间内：

```python
self._max_concurrent: int = min(max(int(max_concurrent), 2), 4)
```

### 4.2 工具过滤与嵌套防护

子代理的工具集通过 allowlist + denylist 过滤：

```python
def _filter_tools(all_tools, allowed, disallowed):
    filtered = list(all_tools)
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]
    return filtered
```

**嵌套 subagent 被显式禁止**：
- `disallowed_tools` 默认包含 `"task"`。
- `task_tool` 加载工具时传入 `subagent_enabled=False`。
- 子代理中间件链不包含 `SubagentLimitMiddleware`。

### 4.3 超时与取消机制

两个项目都采用**合作式取消**（cooperative cancellation）：

```python
# 超时控制
future.result(timeout=self.config.timeout_seconds)

# 取消信号
cancel_event = threading.Event()
result_holder.cancel_event.set()

# 执行循环中检查
async for chunk in agent.astream(...):
    if result_holder.cancel_event.is_set():
        result_holder.try_set_terminal(SubagentStatus.CANCELLED, ...)
        return result_holder
```

> ⚠️ 注意：取消只发生在 `astream` 的迭代边界。如果某个 tool call 本身执行很久（例如一个长时间运行的 bash 命令），在 tool call 完成前无法中断。

### 4.4 上下文隔离与继承

子代理**不是独立的 OS 进程**，而是同一个 Python 进程内的另一个 LangGraph 调用。隔离体现在：
- 独立的 system prompt 和初始消息。
- 独立的工具集。
- 独立的 `max_turns` 和模型配置。

继承体现在：
- `thread_id`：保证沙箱/路径上下文一致。
- `sandbox_state` / `thread_data`：继承父代理的文件系统/工作区状态。
- `trace_id`：用于分布式日志追踪。

### 4.5 错误处理

子代理内部有自己的中间件链，其中关键中间件：

| 中间件 | 作用 |
|--------|------|
| `ToolErrorHandlingMiddleware` | 工具异常转为 `ToolMessage` 错误，而不是崩溃 |
| `DanglingToolCallMiddleware` | 模型发出 tool call 但未收到响应时，自动补 synthetic `ToolMessage` |
| `LLMErrorHandlingMiddleware` | 捕获 LLM 调用异常 |
| `SandboxMiddleware` | 沙箱环境管理 |

子代理中间件链比 Lead Agent **精简很多**，去除了上传、总结、待办、记忆持久化、动态上下文等不相关能力。

### 4.6 Token 用量归因

```python
class SubagentTokenCollector(BaseCallbackHandler):
    def on_llm_end(self, response, *, run_id, ...):
        # 从 generation.message.usage_metadata 提取用量
        # 按 run_id 去重
```

子代理执行完成后，token 用量会回报到父代理的 `RunJournal`，保证一次用户请求的总用量统计包含所有 subagent。

---

## 5. 边界条件与风险点

### 5.1 超时竞争

如果 subagent 恰好在超时前一刻完成，`try_set_terminal` 的锁保证第一个写入的终端状态生效。测试中也明确验证了：
- timeout handler 不会覆盖 `CANCELLED`
- late completion 不会覆盖 `TIMED_OUT`

### 5.2 取消无法中断正在运行的 tool

合作式取消的局限性：长按 `cancel` 只能等到当前 tool call 返回后才会生效。

### 5.3 隔离事件循环单点风险

所有 subagent 共享一个 daemon thread loop。如果某个 subagent 的协程死锁或阻塞，会影响其他 subagent 的执行。项目通过以下方式缓解：
- loop 启动失败 5 秒超时。
- 工具异常由中间件捕获。
- 后台 scheduler pool 与隔离 loop 分离。

### 5.4 并发上限被硬限制

`max_concurrent` 被 clamp 到 `[2, 4]`，即使配置更大也不会生效，这是为了防止资源耗尽。

### 5.5 后台任务记录可能泄漏

 DeerFlow 的 `_background_tasks` 字典会持有结果直到显式 `cleanup_background_task()`。如果取消/异常路径未正确清理，可能累积内存。`task_tool` 中使用了 deferred cleanup 机制来兜底。

### 5.6 未知 subagent 类型

```python
if config is None:
    available = ", ".join(available_subagent_names)
    return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"
```

### 5.7 `bash` subagent 额外安全门

DeerFlow 中 `bash` subagent 需要 `is_host_bash_allowed()` 返回 True，否则返回禁用提示。这是为了防止在不可信环境中暴露本地 shell。

---

## 6. 对比总结

| 特性 | DeerFlow | MultiAgent-Studio |
|------|----------|-------------------|
| 触发方式 | `task` 工具直接指定 `subagent_type` | `create_subagent` + `task` 两步 |
| 配置来源 | 内置 + `config.yaml` | `presets.py` + 运行时创建 |
| 执行模式 | 后台任务 + 父代理轮询 | 同步/后台 + SSE 实时流 |
| 实时性 | 5 秒轮询中间消息 | `asyncio.Queue` 即时推送 |
| 状态管理 | 全局 `_background_tasks` | `SubagentManager` 内部字典 |
| 代码风格 | 简洁、配置驱动 | 更工程化、模块拆分更细 |

两者在底层技术选择上高度一致：
- 都使用 LangGraph `create_agent`。
- 都使用 `astream(stream_mode="values")`。
- 都使用持久化 daemon thread 隔离事件循环。
- 都使用 `threading.Event` 实现合作式取消。
- 都通过 allowlist/denylist 过滤工具并禁止嵌套 subagent。

---

## 7. 实际使用示例

### 7.1 DeerFlow：Lead Agent 调用 `task` 工具

```python
task(
    description="Tencent financial data",
    prompt="""Search for Tencent's latest quarterly financial report.
Summarize revenue, net profit, and key business segments.
Return results in bullet points under 300 words.""",
    subagent_type="general-purpose",
)
```

当一次响应中需要委托多个子任务时，注意每轮最多 3 个并行 subagent：

```python
# 第一批
 task(description="AWS analysis", prompt="...", subagent_type="general-purpose")
 task(description="Azure analysis", prompt="...", subagent_type="general-purpose")
 task(description="GCP analysis", prompt="...", subagent_type="general-purpose")

# 第二批（如果还有剩余）
 task(description="Alibaba Cloud analysis", prompt="...", subagent_type="general-purpose")
```

### 7.2 MultiAgent-Studio：创建并调用 subagent

```python
from harness.agents.subagent_manager import SubagentManager
from harness.agents.presets import build_subagent_config
from harness.tools.registry import ToolRegistry

registry = ToolRegistry()
# ... 加载工具 ...

manager = SubagentManager(
    llm_factory=lambda model: ChatOpenAI(model=model or "gpt-4o"),
    tool_registry=registry,
    max_concurrent=3,
)

config = build_subagent_config("my-coder", "coder")
await manager.create(config)

result = await manager.execute(
    "my-coder",
    instruction="""【Goal】Write a Python function to compute factorial.
【Constraints】Use only the standard library.
【Output Format】Function + 3 test cases.""",
)
print(result.status, result.output)
```

### 7.3 自定义 subagent 配置（ DeerFlow `config.yaml`）

```yaml
subagents:
  custom_agents:
    - name: code-reviewer
      description: Specialist for reviewing Python code
      system_prompt: You are an expert Python code reviewer...
      tools:
        - read_file
        - web_search
      disallowed_tools:
        - task
        - bash
        - ask_clarification
      model: gpt-4o
      max_turns: 30
      timeout_seconds: 600
```

---

## 8. 关键源码摘录

### 8.1 隔离事件循环启动（MultiAgent-Studio）

```python
def _get_isolated_loop() -> asyncio.AbstractEventLoop:
    global _isolated_loop, _isolated_loop_thread
    with _isolated_loop_lock:
        thread_alive = _isolated_loop_thread is not None and _isolated_loop_thread.is_alive()
        loop_usable = (
            _isolated_loop is not None
            and not _isolated_loop.is_closed()
            and _isolated_loop.is_running()
            and thread_alive
        )
        if not loop_usable:
            loop = asyncio.new_event_loop()
            started = threading.Event()
            thread = threading.Thread(
                target=_run_isolated_loop,
                args=(loop, started),
                name="subagent-isolated-loop",
                daemon=True,
            )
            thread.start()
            if not started.wait(timeout=5):
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
                loop.close()
                raise RuntimeError("Timed out starting isolated subagent event loop")
            _isolated_loop = loop
            _isolated_loop_thread = thread
        return _isolated_loop
```

### 8.2 工具过滤（DeerFlow / MultiAgent-Studio 几乎一致）

```python
def _filter_tools(all_tools, allowed, disallowed):
    filtered = list(all_tools)
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]
    return filtered
```

### 8.3 单次写入终止状态（DeerFlow）

```python
def try_set_terminal(self, status, *, result=None, error=None, ...) -> bool:
    if not status.is_terminal:
        raise ValueError(f"Status {status} is not terminal")
    with self._state_lock:
        if self.status.is_terminal:
            return False
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error
        if ai_messages is not None:
            self.ai_messages = ai_messages
        self.completed_at = completed_at or datetime.now()
        self.status = status
        return True
```

---

## 9. 总结

Subagent 的本质是：**在同一个进程内，通过独立的 LangGraph agent 实例 + 独立事件循环 + 工具过滤 + 状态锁，实现“看起来像独立代理”的任务委托机制**。

核心技术点可归纳为：

1. **LangGraph agent 复用**：子代理和主代理共享同一套 graph 构建能力。
2. **持久化隔离事件循环**：解决 async 运行循环冲突和客户端绑定问题。
3. **合作式取消 + 锁保护的终止状态**：处理超时/取消/完成竞争。
4. **工具 allowlist/denylist**：实现权限控制并防止嵌套 subagent。
5. **上下文继承（thread_id/sandbox/thread_data）+ 对话隔离**：保证工作区一致但对话独立。
6. **Token 用量归因**：通过 callback 收集并回报到父代理。

边界条件需要特别关注：

- 取消只能中断到 `astream` 迭代边界，无法中断单个长工具调用。
- 共享的隔离事件循环是单点，死锁会影响所有 subagent。
- 并发上限被硬编码限制在 `[2, 4]`。
- 后台任务记录需要正确清理，否则有内存泄漏风险。
- `bash` subagent 有额外的安全开关。

理解这些原理后，在二次开发或排查 subagent 问题时，可以更有针对性地定位：是配置解析、工具过滤、事件循环提交、取消/超时竞争，还是消息流消费出了问题。
