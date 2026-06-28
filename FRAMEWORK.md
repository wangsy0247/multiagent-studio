# Multiagent-Studio Harness 框架文档

> 本文档用于快速理解项目结构、各文件职责和核心流程，便于后续读写代码时参考。

---

## 1. 项目总览

`multiagent-studio/harness` 是一个基于 **LangGraph + LangChain Agent Middleware** 的多智能体执行框架。

### 核心能力

- 前端通过 API 提交执行图或自然语言请求
- Lead Agent 驱动 ReAct 循环，可调用工具、派发 SubAgent
- 17 层 Middleware 处理错误、记忆、上下文、摘要、工具限制等
- 长期记忆（per-user JSON）+ 会话记忆（LangGraph checkpoint）
- 可选上下文压缩（Summarization）
- 可观测性（Langfuse）、运行日志（RunJournal）、持久化（SQLite/Postgres）

### 技术栈

- LangGraph / LangChain
- FastAPI（API 层）
- Pydantic（模型）
- PyYAML（配置热加载）
- SQLite / Postgres（checkpoint + 应用持久化）

---

## 2. 目录结构

```
harness/
├── main.py                    # 服务入口：初始化、装配、生命周期
├── models.py                  # Pydantic 模型 + HarnessState 定义
├── graph_factory.py           # 用 create_agent() 编译 Lead Agent 图
├── config/                    # 配置系统
│   ├── __init__.py            # HarnessConfig（.env 配置）
│   ├── config_manager.py      # YAML 配置热加载
│   ├── yaml_config.py         # YAML 配置模型
│   ├── memory_config.py       # 记忆配置
│   ├── summarization_config.py# 摘要压缩配置
│   ├── checkpointer_config.py # checkpoint 后端配置
│   ├── tool_config.py         # 工具配置
│   └── paths.py               # 数据路径管理
├── api/                       # FastAPI 服务
│   ├── server.py              # ASGI 应用、HarnessService 基类
│   └── routers.py             # API 路由（/execute、/clarification 等）
├── agents/                    # Agent 实现
│   ├── lead_agent.py          # Lead Agent 配置/工具/提示词提供者
│   ├── subagent.py            # 单个子代理实现
│   ├── subagent_manager.py    # 子代理并发管理
│   ├── features.py            # 运行时特性开关
│   └── presets.py             # 预设 agent 配置
├── middleware/                # 17 层 Middleware
│   ├── __init__.py            # AGENT_MIDDLEWARE_ORDER
│   ├── base.py                # HarnessAgentMiddleware 基类
│   ├── dynamic_context.py     # 记忆/日期注入
│   ├── summarization.py       # 上下文压缩
│   ├── memory.py              # 记忆更新队列
│   ├── token_usage.py         # Token 统计
│   ├── todo.py                # Plan Mode TODO
│   ├── title.py               # 自动标题
│   ├── llm_error.py           # LLM 错误重试
│   ├── loop_detection.py      # 循环检测
│   ├── guardrail.py           # 安全护栏
│   ├── tool_error.py          # 工具错误处理
│   ├── clarification.py       # 澄清请求
│   ├── view_image.py          # 图片处理
│   ├── subagent_limit.py      # 子代理并发限制
│   ├── dangling_tool_call.py  # 悬空工具调用修复
│   ├── thread_data.py         # 线程目录
│   ├── uploads.py             # 上传文件处理
│   └── sandbox.py             # 沙箱执行
├── memory/                    # 长期记忆系统
│   ├── storage.py             # 文件存储（JSON）
│   ├── updater.py             # LLM 驱动记忆更新
│   ├── queue.py               # 异步 debounce 更新队列
│   ├── prompt.py              # 记忆注入/更新提示词
│   ├── message_processing.py  # 消息过滤、信号检测
│   └── summarization_hook.py  # 摘要前 flush 记忆
├── tools/                     # 工具注册与实现
│   ├── registry.py            # ToolRegistry
│   ├── builtins/              # 内置工具
│   ├── mcp_adapter.py         # MCP 工具适配
│   ├── sandbox_tools.py       # 沙箱工具
│   └── search.py              # 搜索工具
├── runtime/                   # 运行时基础设施
│   ├── checkpointer/          # LangGraph checkpoint 提供者
│   ├── journal.py             # RunJournal（运行事件日志）
│   ├── events/store/          # 事件存储
│   └── runs/store/            # Run 元数据存储
├── persistence/               # 数据库 ORM/引擎
├── observability/             # Langfuse 集成
├── services/                  # 沙箱提供者
├── prompts/                   # 提示词模板
└── tests/                     # 测试
```

---

## 3. 启动流程

### 3.1 入口

**文件**：`harness/main.py`

```python
if __name__ == "__main__":
    uvicorn.run(...)
```

### 3.2 初始化链

```text
1. python -m harness.main
   └── 创建 HarnessService(config, config_manager)

2. HarnessService.initialize()  （首次请求时懒加载）
   ├── set_paths()              # 初始化数据目录
   ├── _init_llm()              # 创建 Lead Agent LLM
   ├── tool_registry 加载工具  # YAML tools + MCP
   ├── MemoryConfig 单例初始化  # FileMemoryStorage + MemoryUpdateQueue
   ├── ObservabilityManager     # Langfuse
   ├── JudgeEvaluator           # 评价 LLM
   ├── _register_middlewares()  # 按顺序装配 17 层 middleware
   ├── SubagentManager          # 子代理并发池
   ├── LeadAgent                # 系统提示词 + 工具列表
   ├── AsyncCheckpointerProvider# 创建 checkpoint saver
   └── build_harness_graph()    # create_agent() 编译图

3. api/server.py
   └── set_harness(service) 后启动 ASGI
```

### 3.3 关键初始化函数

| 函数 | 文件 | 作用 |
|---|---|---|
| `HarnessService.__init__()` | `main.py:79` | 创建 service，加载 HarnessConfig + ConfigManager |
| `HarnessService.initialize()` | `main.py:119` | 完整装配所有组件 |
| `_init_llm()` | `main.py:265` | 用 ChatOpenAI 创建 LLM |
| `_register_middlewares()` | `main.py:338` | 按 `AGENT_MIDDLEWARE_ORDER` 创建 middleware 实例 |
| `build_harness_graph()` | `graph_factory.py:97` | 调用 `create_agent()` 编译 LangGraph |

---

## 4. 配置系统

### 4.1 两层配置

| 配置来源 | 文件 | 优先级 | 用途 |
|---|---|---|---|
| `.env` | `harness/.env` | 高 | LLM、端口、密钥等环境变量 |
| `config.yaml` | `harness/config.yaml` | 中 | 特性开关、工具、记忆、摘要等 |

### 4.2 HarnessConfig

**文件**：`harness/config/__init__.py`

从 `.env` 读取，关键字段：

- `default_model` / `judge_model` / `title_model` / `summary_model`
- `openai_api_key` / `openai_base_url`
- `data_root` / `workspace_root` / `memory_root`
- `mcp_config_path`
- Langfuse 配置

### 4.3 ConfigManager

**文件**：`harness/config/config_manager.py`

- 加载 `config.yaml`
- 支持 `$VAR` 环境变量插值
- mtime 轮询热加载（3 秒间隔）
- 注册 `on_change` 回调

### 4.4 各子配置

| 配置类 | 文件 | 作用 |
|---|---|---|
| `MemoryConfig` | `config/memory_config.py` | 记忆开关、存储路径、debounce、注入 token 预算 |
| `SummarizationConfig` | `config/summarization_config.py` | 摘要触发条件、保留策略、skill rescue、dynamic context 保护 |
| `CheckpointerConfig` | `config/checkpointer_config.py` | checkpoint 后端（memory/sqlite/postgres） |
| `ToolConfig` | `config/tool_config.py` | 工具加载配置 |

---

## 5. Agent 执行流程

### 5.1 单次执行完整链路

**入口**：`harness/main.py::HarnessService.execute()`

```text
1. 接收参数
   thread_id / user_id / message / execution_graph / files

2. 懒初始化
   if not self._initialized:
       await self.initialize()

3. 构建 RunnableConfig
   _build_config(thread_id)
   └── 注入 callbacks（Langfuse、RunJournal）

4. 从 checkpoint 恢复状态
   state_snapshot = await self.graph.aget_state(build_config)
   ├── 新会话
   │   └── current_state = initial_state(thread_id, user_id, message, files)
   └── 老会话
       ├── 校验 user_id 归属
       └── current_state["messages"] += [new_human_message]

5. 选择执行图
   ├── execution_graph 非空 → runner = _build_custom_graph(graph)
   └── 默认 → runner = self.graph

6. 注册运行期标记
   self._active_runs[thread_id] = {"cancelled": False}
   _enforce_capacity()  # 并发上限 1000

7. 流式执行
   async for event in runner.astream_events(current_state, build_config, version="v2"):
       ├── on_chat_model_stream
       │   └── yield {"type": "message", "content": token}
       ├── on_chat_model_end
       │   └── yield {"type": "token_usage", "tokens": {...}}
       ├── on_tool_start
       │   ├── tool_name == "task" → yield subagent_start
       │   └── 其他 → yield tool_call
       ├── on_tool_end
       │   ├── tool_name == "task" → yield subagent_end
       │   └── 其他 → yield tool_result
       └── on_chain_end
           └── 检查 final_state → yield finished / clarification / title_update

8. 后处理
   ├── log_token_usage() → Langfuse
   ├── update_run_completion() → RunStore
   ├── _cleanup_middleware_state(thread_id)
   └── self._active_runs.pop(thread_id)
```

### 5.2 核心执行函数

| 函数 | 文件 | 作用 |
|---|---|---|
| `HarnessService.execute()` | `main.py:531` | 主执行入口，流式返回 SSE 事件 |
| `HarnessService._build_config()` | `main.py:476` | 构建带 callbacks 的 RunnableConfig |
| `initial_state()` | `models.py:281` | 创建新的 HarnessState |
| `build_harness_graph()` | `graph_factory.py:97` | 编译 Lead Agent 图 |
| `HarnessGraphFactory.build()` | `graph_factory.py:64` | 用 create_agent() 构建内层图 + StateGraph 外层 |

### 5.3 图的构建

**文件**：`harness/graph_factory.py`

```python
def build_harness_graph(llm, tools, middlewares, system_prompt, checkpointer):
    # 内层：create_agent 处理 ReAct 循环
    inner_agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middlewares,
        state_schema=HarnessState,
    )

    # 外层：START → agent → END
    graph = StateGraph(HarnessState)
    graph.add_node("agent", inner_agent)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)

    return graph.compile(checkpointer=checkpointer)
```

### 5.4 澄清回复

**入口**：`harness/main.py::HarnessService.respond_to_clarification()`

```text
1. aget_state(thread_id) 读取暂停状态
2. 检查 pending_clarification 是否存在
3. 更新 pending_clarification.answer
4. self._active_runs[thread_id] = {"cancelled": False}
5. astream_events(state, build_config, version="v2")
6. 若仍有新 clarification → yield clarification
   否则 → yield finished
```

### 5.5 SubAgent 执行

**入口**：`harness/agents/subagent.py::SubAgent.execute()`

```text
1. 构造消息列表
   messages = [SystemMessage(context)?, HumanMessage(instruction)]

2. 继承 thread_id / user_id（如果提供 parent_state）

3. 调用 self._graph.ainvoke(state, RunnableConfig())

4. 解析结果
   ├── 最后一条是 AIMessage 且无 tool_calls → status="success"
   └── 最后一条是 AIMessage 带 tool_calls → status="max_iterations_reached"

5. 返回 SubAgentResult(status, output, iterations)
   父 agent 通过 ToolMessage 接收结果
```

**SubAgent 不共享父 checkpoint**：每个 SubAgent 是独立的 `create_agent()` 图，只接收 `instruction` 和 `context`，执行完即销毁。

---

## 6. Middleware 体系

### 6.1 基类

**文件**：`harness/middleware/base.py`

```python
class HarnessAgentMiddleware(AgentMiddleware[HarnessState, Any, Any]):
    state_schema: type[HarnessState] = HarnessState
    name: str = "harness_base"

    def __init__(self, config: dict | None = None)

    # 可覆写钩子
    async def abefore_agent(state, runtime) -> dict | None
    async def aafter_agent(state, runtime) -> dict | None
    async def abefore_model(state, runtime) -> dict | None
    async def aafter_model(state, runtime) -> dict | None
    async def awrap_model_call(request, handler)
    async def awrap_tool_call(request, handler)
```

**重要**：基类故意不提供默认 async 实现。`create_agent()` 通过检查子类是否覆写了钩子来决定是否添加对应节点。

### 6.2 注册顺序

**文件**：`harness/middleware/__init__.py`

`AGENT_MIDDLEWARE_ORDER` 定义了 17 层 middleware 的注册顺序。

```text
[0]  ThreadDataMiddleware      # 线程目录准备
[1]  UploadsMiddleware         # 上传文件处理
[2]  SandboxMiddleware         # 沙箱初始化
─────────────────────────────────────
wrap_model_call 洋葱（外层 → 内层）
[3]  LLMErrorHandlingMiddleware   # LLM 错误重试（最外层）
[4]  LoopDetectionMiddleware      # 循环检测
[5]  DanglingToolCallMiddleware   # 悬空工具调用修复（最内层）
─────────────────────────────────────
[6]  GuardrailMiddleware       # 安全护栏
[7]  ToolErrorHandlingMiddleware # 工具错误处理
[8]  DynamicContextMiddleware  # 记忆/日期注入（abefore_agent）
[9]  SummarizationMiddleware   # 上下文压缩（abefore_model）
[10] TodoMiddleware            # Plan Mode TODO
[11] TokenUsageMiddleware      # Token 统计（aafter_model）
[12] TitleMiddleware           # 自动标题（aafter_model）
[13] MemoryMiddleware          # 记忆更新队列（aafter_agent）
[14] ViewImageMiddleware       # 图片处理
[15] SubagentLimitMiddleware   # 子代理并发限制
[16] ClarificationMiddleware   # 澄清请求（always last）
```

### 6.3 Hook 执行规则

- `abefore_agent` / `abefore_model`：**正向**执行（index 0 → N）
- `aafter_model` / `aafter_agent`：**反向**执行（index N → 0）
- `awrap_model_call`：**嵌套**执行（后注册 = 外层 wrapper）

### 6.4 各 Middleware 详细说明

#### ThreadDataMiddleware

**文件**：`middleware/thread_data.py`

- `abefore_agent()`：确保线程工作目录存在
- 在 workspace_root 下创建 `users/{user_id}/threads/{thread_id}/`

#### UploadsMiddleware

**文件**：`middleware/uploads.py`

- `abefore_agent()`：处理上传文件，写入线程目录
- 支持最大文件数/大小限制

#### SandboxMiddleware

**文件**：`middleware/sandbox.py`

- `abefore_agent()`：初始化沙箱 provider
- 支持 LocalSandboxProvider 和 OpenSandboxProvider

#### LLMErrorHandlingMiddleware

**文件**：`middleware/llm_error.py`

- `awrap_model_call()`：捕获 LLM 调用异常，按错误码重试
- 默认最多 4 次重试

#### LoopDetectionMiddleware

**文件**：`middleware/loop_detection.py`

- `abefore_agent()` / `awrap_model_call()`：检测模型输出重复模式
- 超过阈值时抛出异常或标记 `loop_detected`

#### DanglingToolCallMiddleware

**文件**：`middleware/dangling_tool_call.py`

- `awrap_model_call()`：修复模型只发 tool_calls 但不等待结果的情况

#### GuardrailMiddleware

**文件**：`middleware/guardrail.py`

- `abefore_agent()`：执行安全策略检查
- 可配置 allowlist/denylist

#### ToolErrorHandlingMiddleware

**文件**：`middleware/tool_error.py`

- `awrap_tool_call()`：捕获工具执行异常，转换为 ToolMessage
- 支持 max_retries 配置

#### DynamicContextMiddleware

**文件**：`middleware/dynamic_context.py`

- `abefore_agent()`：注入记忆和当前日期
- `_build_full_reminder()`：构建 `<system-reminder>`
- `_inject()`：决定首次/同日/跨天注入策略
- `is_dynamic_context_reminder()`：识别 reminder 消息

#### SummarizationMiddleware

**文件**：`middleware/summarization.py`

- `abefore_model()`：检查并触发上下文压缩
- `_maybe_summarize()` / `_amaybe_summarize()`：核心压缩逻辑
- `_preserve_dynamic_context_reminders()`：保护 reminder 不被压缩
- `_fire_hooks()`：摘要前触发 memory flush hook

#### TodoMiddleware

**文件**：`middleware/todo.py`

- `abefore_agent()`：plan_mode 下无 todo 时标记 `context_lost`
- `aafter_model()`：所有 todo 完成时设置 `plan_mode_exit`

#### TokenUsageMiddleware

**文件**：`middleware/token_usage.py`

- `aafter_model()`：从 AIMessage.usage_metadata 提取 token 使用量
- 累加到 `state["token_usage"]` dict

#### TitleMiddleware

**文件**：`middleware/title.py`

- `aafter_model()`：首轮对话后调用轻量模型生成标题
- 结果存入 `state["suggested_title"]`

#### MemoryMiddleware

**文件**：`middleware/memory.py`

- `aafter_agent()`：过滤 human/ai 消息，检测纠错/强化信号
- 调用 `MemoryUpdateQueue.add()` 入队更新

#### ViewImageMiddleware

**文件**：`middleware/view_image.py`

- `abefore_model()`：处理多模态图片消息

#### SubagentLimitMiddleware

**文件**：`middleware/subagent_limit.py`

- `awrap_tool_call()`：限制并发子代理数量

#### ClarificationMiddleware

**文件**：`middleware/clarification.py`

- `aafter_model()`：检测是否需要用户澄清
- 生成 `pending_clarification` 并暂停执行

---

## 7. Memory 长期记忆系统

### 7.1 存储层

**文件**：`harness/memory/storage.py`

#### 文件布局

```text
{memory_root}/users/{user_id}/memory.json
{memory_root}/users/{user_id}/agents/{agent_name}/memory.json  # 可选 per-agent
```

#### 核心类

| 类/函数 | 作用 |
|---|---|
| `FileMemoryStorage` | 基于 JSON 文件的 per-user 记忆存储 |
| `load()` | 带 mtime 缓存的加载 |
| `reload()` | 强制绕过缓存重新加载 |
| `save()` | 写临时文件 + `fcntl` 文件锁 + 原子替换 |
| `get_memory_storage()` | 全局单例 |
| `create_empty_memory()` | 创建空记忆结构 |

#### 数据格式

```json
{
  "version": "1.0",
  "lastUpdated": "2026-06-27T10:00:00Z",
  "user": {
    "workContext": {"summary": "...", "updatedAt": "..."},
    "personalContext": {"summary": "...", "updatedAt": "..."},
    "topOfMind": {"summary": "...", "updatedAt": "..."}
  },
  "history": {
    "recentMonths": {"summary": "...", "updatedAt": "..."},
    "earlierContext": {"summary": "...", "updatedAt": "..."},
    "longTermBackground": {"summary": "...", "updatedAt": "..."}
  },
  "facts": [
    {"id": "fact_xxx", "content": "...", "category": "...", "confidence": 0.9, "createdAt": "...", "source": "thread_id"}
  ]
}
```

#### 缓存策略

```python
_memory_cache[(user_id, agent_name)] = (data, mtime)
```

- 加载时比较文件 mtime 和缓存 mtime
- 文件变更时自动失效
- 写操作时更新缓存

### 7.2 更新队列

**文件**：`harness/memory/queue.py`

#### 核心类

| 类/函数 | 作用 |
|---|---|
| `MemoryUpdateQueue` | 异步 debounce 更新队列 |
| `add()` | 加入队列，debounce 后处理（默认 30s） |
| `add_nowait()` | 立即处理（0 debounce） |
| `flush()` | 取消 pending task，立即处理（shutdown 用） |
| `clear()` | 清空队列不处理 |
| `get_memory_queue()` | 全局单例 |

#### 合并规则

以 `(thread_id, user_id, agent_name)` 为 key：

- 同一 key 多次 `add()` 会合并为一次更新
- `correction_detected` / `reinforcement_detected` 取 OR

#### 处理流程

```text
add() / add_nowait()
  └── _enqueue_locked()        # 合并/替换同 key 的 context
  └── _ensure_task(debounce)   # 创建 asyncio debounce task
      └── _debounced_process()
          └── await asyncio.sleep(debounce)
          └── _process_queue()
              ├── 取出所有 contexts
              ├── for context in contexts:
              │     await MemoryUpdater.aupdate_memory(...)
              │     await asyncio.sleep(0.5)  # 避免突发 LLM 调用
              └── processing = False
```

### 7.3 记忆更新器

**文件**：`harness/memory/updater.py`

#### 核心类

| 类/函数 | 作用 |
|---|---|
| `MemoryUpdater` | 用 LLM 分析对话并更新记忆 |
| `aupdate_memory()` | 异步更新入口 |
| `_prepare_update_prompt()` | 构造更新 prompt |
| `_compact_memory_for_prompt()` | facts > 25 时只保留 top 25 |
| `_apply_updates()` | 合并 LLM 返回的 delta 到当前记忆 |
| `_strip_upload_mentions_from_memory()` | 去除文件上传相关内容 |
| `get_memory_data()` | 读取记忆数据 |
| `clear_memory_data()` | 清空记忆 |

#### 完整更新流程

```text
MemoryUpdater.aupdate_memory(messages, thread_id, user_id)
  └── _do_update_memory()
      ├── _prepare_update_prompt()
      │   ├── get_memory_data(agent_name, user_id=user_id)
      │   ├── format_conversation_for_update(messages)
      │   │   └── filter_messages_for_memory()  # 只保留 human/ai 最终回复
      │   ├── _compact_memory_for_prompt()
      │   └── MEMORY_UPDATE_PROMPT.format(...)
      ├── model.ainvoke(prompt, config={"run_name": "memory_agent"})
      ├── _finalize_update()
      │   ├── json.loads(response.content)
      │   ├── _apply_updates(current_memory, update_data, thread_id)
      │   │   ├── 更新 user.* summaries
      │   │   ├── 更新 history.* summaries
      │   │   ├── 删除 factsToRemove
      │   │   ├── 添加 newFacts（去重、置信度阈值、max_facts）
      │   │   └── 按置信度截断到 max_facts
      │   ├── _strip_upload_mentions_from_memory()
      │   └── FileMemoryStorage.save(updated_memory)
      └── return True/False
```

#### `_apply_updates` 规则

- `user` 和 `history` 下的每个 section：仅当 LLM 返回 `shouldUpdate=true` 时更新
- `factsToRemove`：按 `id` 删除事实
- `newFacts`：
  - 置信度 ≥ `fact_confidence_threshold`（默认 0.7）
  - 按内容去重（casefold）
  - 生成 `fact_{uuid}` id
  - 记录 `source=thread_id`
- 超过 `max_facts`（默认 100）时按置信度排序截断

### 7.4 消息处理

**文件**：`harness/memory/message_processing.py`

| 函数 | 作用 |
|---|---|
| `filter_messages_for_memory()` | 只保留 human 输入和最终 ai 回复，跳过 tool calls |
| `detect_correction()` | 检测用户纠错信号（"不对"、"你理解错了"等） |
| `detect_reinforcement()` | 检测正向强化信号（"完全正确"、"就是这样"等） |
| `extract_message_text()` | 从 message content 提取纯文本 |

### 7.5 注入流程

**文件**：`harness/memory/prompt.py`、`harness/middleware/dynamic_context.py`

#### 注入函数

| 函数 | 文件 | 作用 |
|---|---|---|
| `DynamicContextMiddleware._inject()` | `dynamic_context.py:148` | 主注入逻辑 |
| `_build_full_reminder()` | `dynamic_context.py:89` | 构建完整 reminder |
| `_build_date_update_reminder()` | `dynamic_context.py:116` | 只更新日期 |
| `format_memory_for_injection()` | `prompt.py:126` | 把记忆格式化为注入文本 |

#### 注入策略

```text
_last_injected_date(messages) is None:
    └── 首次对话
        ├── 找到第一个用户消息索引
        ├── _build_full_reminder(user_id)
        │   ├── get_memory_data(agent_name, user_id=user_id)
        │   ├── format_memory_for_injection(memory_data, max_tokens=...)
        │   └── 包装成 <system-reminder><memory>...</memory><current_date>...</current_date></system-reminder>
        └── 把 reminder 插入到用户消息前

_last_injected_date == current_date:
    └── 同一天，不注入

_last_injected_date != current_date:
    └── 跨天
        ├── _build_date_update_reminder()
        └── 插入到最后一个用户消息前
```

#### 注入消息格式

```text
<system-reminder>
<memory>
User Context:
- Work: ...
- Personal: ...
- Current Focus: ...

History:
- Recent: ...

Facts:
- [preference | 0.90] ...
</memory>

<current_date>2026-06-27, Saturday</current_date>
</system-reminder>
```

#### token 预算

`max_injection_tokens` 默认 2000，按以下优先级填充：

1. User Context（work/personal/topOfMind）
2. History（recent/earlier/background）
3. Facts（按置信度降序）

超出预算时截断并加 `...`。

### 7.6 摘要前记忆 flush

**文件**：`harness/memory/summarization_hook.py`

```python
def memory_flush_hook(event: SummarizationEvent):
    filtered = filter_messages_for_memory(event.messages_to_summarize)
    queue.add_nowait(
        thread_id=event.thread_id,
        messages=filtered,
        agent_name=event.agent_name,
        user_id=resolve_user_id(event.runtime),
        correction_detected=detect_correction(filtered),
        reinforcement_detected=detect_reinforcement(filtered),
    )
```

作用：在 summarization 丢弃老消息前，先把它们写进长期记忆。

---

## 8. 上下文压缩（Summarization）

### 8.1 配置

**文件**：`harness/config/summarization_config.py`

| 配置 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `enabled` | bool | true | 开关 |
| `model_name` | str \| None | None | 摘要模型（None 用默认模型） |
| `trigger` | ContextSize \| list | None | 触发条件（OR 语义） |
| `keep` | ContextSize | messages:20 | 压缩后保留多少 |
| `trim_tokens_to_summarize` | int \| None | 4000 | 摘要调用前消息截断 token 数 |
| `summary_prompt` | str \| None | None | 自定义摘要 prompt |
| `preserve_dynamic_context_reminders` | bool | true | 保护 dynamic context reminders |
| `preserve_recent_skill_count` | int | 5 | 保留最近 skill 数量 |
| `preserve_recent_skill_tokens` | int | 25000 | skill rescue 总 token 预算 |
| `preserve_recent_skill_tokens_per_skill` | int | 5000 | 单个 skill token 上限 |
| `skill_file_read_tool_names` | list[str] | read_file/read/view/cat | 视为 skill 读取的工具名 |

### 8.2 触发逻辑

**文件**：`harness/middleware/summarization.py`

每次 LLM 调用前检查：`before_model()` / `abefore_model()`

触发条件（`_should_summarize()`）：

- `type=messages, value=N`：消息数 ≥ N
- `type=tokens, value=N`：
  - 估算总 token ≥ N，或
  - 最后一条 AIMessage.usage_metadata.total_tokens ≥ N
- `type=fraction, value=0.x`：达到模型最大输入 token 的 x%

### 8.3 压缩流程

```text
_maybe_summarize(state, runtime)
  ├── messages = list(state["messages"])
  ├── _ensure_message_ids(messages)
  ├── total_tokens = token_counter(messages)
  ├── _should_summarize(messages, total_tokens)
  │   └── 未触发 → return None
  ├── cutoff_index = _determine_cutoff_index(messages)
  │   └── cutoff_index <= 0 → return None
  ├── messages_to_summarize, preserved = _partition_messages(messages, cutoff_index)
  ├── messages_to_summarize, preserved = _preserve_dynamic_context_reminders(...)
  ├── _fire_hooks(messages_to_summarize, preserved, runtime)
  │   └── memory_flush_hook() → MemoryUpdateQueue.add_nowait()
  ├── summary = _create_summary(messages_to_summarize)
  │   ├── _trim_messages_for_summary()  # 截断到 trim_tokens_to_summarize
  │   ├── get_buffer_string(trimmed_messages)
  │   └── model.invoke(prompt)
  ├── new_messages = _build_new_messages(summary)
  │   └── HumanMessage(content="Here is a summary...", name="summary")
  └── return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *new_messages,
            *preserved,
        ]
      }
```

### 8.4 切割策略

**父类 LangChain SummarizationMiddleware 实现**

#### `_determine_cutoff_index()`

```text
if keep.type in {tokens, fraction}:
    token_based_cutoff = _find_token_based_cutoff(messages)
    if token_based_cutoff is not None:
        return token_based_cutoff
    return _find_safe_cutoff(messages, _DEFAULT_MESSAGES_TO_KEEP)
else:  # messages
    return _find_safe_cutoff(messages, keep.value)
```

#### `_find_safe_cutoff()`

```text
if len(messages) <= messages_to_keep:
    return 0

target_cutoff = len(messages) - messages_to_keep
return _find_safe_cutoff_point(messages, target_cutoff)
```

例如 50 条消息，保留 10 条：`target_cutoff = 40`，前 40 条进入摘要。

#### `_find_safe_cutoff_point()`

如果 `cutoff_index` 落在 `ToolMessage` 上：

1. 向后收集连续 ToolMessage 的 `tool_call_id`
2. 向前查找对应的 `AIMessage`（含匹配的 `tool_calls`）
3. 把 cutoff 移到该 `AIMessage` 开头，保留完整 AI/Tool 对

### 8.5 Dynamic Context Reminder 保护

**文件**：`harness/middleware/summarization.py:116`

```python
def _preserve_dynamic_context_reminders(self, to_summarize, preserved):
    if not self._preserve_dynamic_context_reminders_enabled:
        return to_summarize, preserved

    reminders = [msg for msg in to_summarize if is_dynamic_context_reminder(msg)]
    if not reminders:
        return to_summarize, preserved

    remaining = [msg for msg in to_summarize if not is_dynamic_context_reminder(msg)]
    return remaining, reminders + preserved
```

把 dynamic context reminder 从"待压缩"移到"保留"队列最前面。

### 8.6 摘要生成

**父类实现**

```python
def _create_summary(self, messages_to_summarize):
    trimmed = self._trim_messages_for_summary(messages_to_summarize)
    formatted = get_buffer_string(trimmed)
    response = self.model.invoke(
        self.summary_prompt.format(messages=formatted).rstrip(),
        config={"metadata": {"lc_source": "summarization"}},
    )
    return response.text.strip()
```

默认 prompt 要求输出四个部分：

- SESSION INTENT
- SUMMARY
- ARTIFACTS
- NEXT STEPS

### 8.7 替换消息

```python
return {
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),  # 清空所有旧消息
        HumanMessage(content=f"Here is a summary...\n\n{summary}", name="summary"),
        *preserved_messages,                     # 保留的最近消息
    ]
}
```

LangGraph 的 `add_messages` reducer 会：

1. 先执行 `RemoveMessage(REMOVE_ALL_MESSAGES)` 删除现有 messages
2. 添加摘要消息
3. 添加保留消息

### 8.8 当前 multiagent-studio 与 DeerFlow 的差异

| 特性 | multiagent-studio | DeerFlow |
|---|---|---|
| 基础压缩 | ✅ LangChain SummarizationMiddleware | ✅ |
| before_summarization hooks | ✅ memory_flush_hook | ✅ |
| dynamic context reminder 保护 | ✅（本次新增） | ✅ |
| skill rescue | ❌ 未实现 | ✅ 有完整实现 |

如需 skill rescue，可参考 `deer-flow/backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` 移植。

---

## 9. 持久化

### 9.1 Checkpoint 持久化

**文件**：`harness/runtime/checkpointer/async_provider.py`

#### 核心类

| 类/函数 | 作用 |
|---|---|
| `AsyncCheckpointerProvider` | checkpoint saver 工厂 |
| `get_checkpointer()` | 创建并返回 saver（幂等） |
| `_create_memory()` | MemorySaver（内存） |
| `_create_sqlite()` | AsyncSqliteSaver（SQLite） |
| `_create_postgres()` | AsyncPostgresSaver（PostgreSQL） |
| `_create_harness_serde()` | 自定义 JsonPlusSerializer |

#### 后端选择

```yaml
checkpointer:
  backend: sqlite      # memory | sqlite | postgres
  sqlite_dir: ""       # 空 = ~/.multiagent-studio/data/
  postgres_url: ""
```

#### 自定义 Serde

```python
HARNESS_MSGPACK_TYPES = [
    ClarificationRequest,
    EvaluationResult,
    SubAgentResult,
    TodoItem,
    TokenUsage,  # 注意：当前已改为 dict，但保留兼容
]

def _create_harness_serde():
    return JsonPlusSerializer(allowed_msgpack_modules=HARNESS_MSGPACK_TYPES)
```

用途：让 checkpoint 能够序列化/反序列化 Harness 自定义的 Pydantic 类型。

#### Checkpoint 内容

保存完整 `HarnessState`：

- `messages`
- `thread_id` / `user_id`
- `plan_mode` / `todos`
- `memory_context`
- `pending_clarification`
- `subagent_results`
- `token_usage`
- `suggested_title`
- `loop_history`
- `artifacts`

#### 生命周期

```text
HarnessService.initialize()
  └── ckp_provider = AsyncCheckpointerProvider(ckp_cfg)
      └── self._checkpointer = await ckp_provider.get_checkpointer()

HarnessService.shutdown()
  └── await ckp_provider.close()
      ├── 关闭 DB 连接
      └── 调用 saver.aclose()
```

### 9.2 Run/Event 持久化

#### DatabaseEngine

**文件**：`harness/persistence/engine.py`

| 类/函数 | 作用 |
|---|---|
| `DatabaseEngine` | 数据库引擎封装 |
| `init_tables()` | 初始化表结构 |
| `close()` | 关闭连接 |

配置：

```yaml
database:
  backend: sqlite      # memory | sqlite | postgres
  sqlite_dir: ""
  postgres_url: ""
```

#### Run Store

**文件**：`harness/runtime/runs/store/`

| 类 | 作用 |
|---|---|
| `RunStore`（协议） | run 元数据存储接口 |
| `InMemoryRunStore` | 内存实现 |
| `SqliteRunStore` | SQLite 实现 |

主要操作：

- `create_run()`
- `update_run_completion()`
- `update_status()`
- `get_run()` / `list_runs()`

#### Event Store

**文件**：`harness/runtime/events/store/`

| 类 | 作用 |
|---|---|
| `RunEventStore`（协议） | 事件存储接口 |
| `InMemoryEventStore` | 内存实现 |
| `SqliteEventStore` | SQLite 实现 |

RunJournal 通过事件 store 记录运行中的事件。

### 9.3 RunJournal

**文件**：`harness/runtime/journal.py`

| 类/函数 | 作用 |
|---|---|
| `RunJournal` | 记录单次 run 的事件流 |
| `on_llm_end()` | 记录 LLM 响应和 token 使用 |
| `on_tool_start()` / `on_tool_end()` | 记录工具调用 |
| `flush()` | 把事件写入 EventStore |
| `get_completion_data()` | 获取运行完成数据 |

### 9.4 数据目录

```text
~/.multiagent-studio/
├── data/
│   └── deerflow.db           # checkpoint + app persistence
├── workspace/
│   └── users/{user_id}/threads/{thread_id}/
│       ├── user-data/
│       │   └── uploads/      # 上传文件
│       └── outputs/          # 输出文件
├── memory/
│   └── users/{user_id}/memory.json
└── runs/                     # 运行时数据（如使用文件存储）
```

---

## 10. 工具系统

### 10.1 工具注册中心

**文件**：`harness/tools/registry.py`

#### 核心类

| 类/函数 | 作用 |
|---|---|
| `ToolRegistry` | 工具注册中心 |
| `load_tools_from_config(tools_config)` | 从 config.yaml 加载内置工具 |
| `load_plugins_from_config(plugins)` | 从任意 Python 模块加载插件工具 |
| `load_mcp_tools(mcp_config_path)` | 加载 MCP 服务器工具 |
| `get_tool(name)` | 按名称获取工具 |
| `get_tools(names)` | 按名称列表获取工具 |
| `get_all_tools()` | 获取所有已注册工具 |

#### 初始化流程

```text
HarnessService.initialize()
  ├── tool_registry = ToolRegistry()
  ├── tool_registry.load_tools_from_config(tool_configs)   # YAML tools
  ├── tool_registry.load_plugins_from_config(plugin_tools) # 插件
  └── await tool_registry.load_mcp_tools(cfg.mcp_config_path)  # MCP
```

### 10.2 工具配置格式

**文件**：`harness/config/tool_config.py`

```yaml
tools:
  - name: web_search
    group: search
    use: harness.tools.search:web_search

  - name: bash
    group: code
    use: harness.tools.sandbox_tools:bash
```

`use` 格式：`module.path:variable_name`

### 10.3 Lead Agent 工具

**文件**：`harness/tools/builtins/lead_tools.py`

| 工具 | 函数 | 作用 |
|---|---|---|
| `task` | `task_tool` | 派发 SubAgent，传入 instruction 和可选 context |
| `ask_clarification` | `ask_clarification_tool` | 向用户请求澄清 |
| `present_files` | `present_files_tool` | 展示文件给用户 |

`task` 工具会触发 SubagentManager.dispatch() → SubAgent.execute()。

### 10.4 沙箱/文件工具

**文件**：`harness/tools/sandbox_tools.py`

| 工具 | 作用 |
|---|---|
| `bash` | 在沙箱中执行 shell 命令 |
| `file_read` | 读取文件内容 |
| `file_write` | 写入文件 |
| `list_files` | 列出目录内容 |
| `glob_tool` | 文件 glob 匹配 |
| `grep_tool` | 文本搜索 |
| `str_replace` | 字符串替换 |

这些工具通过 SandboxProvider 在隔离环境（Docker/本地沙箱）中执行。

### 10.5 搜索工具

**文件**：`harness/tools/search.py`

| 工具 | 作用 |
|---|---|
| `web_search` | 网络搜索 |
| `arxiv_search` | arXiv 论文搜索 |
| `web_fetch` | 抓取网页内容 |

### 10.6 MCP 工具

**文件**：`harness/tools/mcp_adapter.py`

```text
load_mcp_tools(extensions_config.json)
  └── 读取 MCP 服务器配置
      └── 对每个 server 建立 stdio/sse 连接
          └── 把 server 提供的 tools 包装为 LangChain BaseTool
```

### 10.7 工具权限

**文件**：`harness/config/__init__.py` 中的 `tool_permissions` 和 `default_tool_policy`

可以配置：

```python
tool_permissions = {
    "bash": {"policy": "deny"},
    "file_write": {"policy": "allow"},
}
default_tool_policy = "allow"  # 或 "deny"
```

### 10.8 工具调用流程

```text
1. LLM 输出 AIMessage(tool_calls=[...])
2. create_agent 工具节点执行每个 tool_call
3. awrap_tool_call() 洋葱（ToolErrorHandling → SubagentLimit → ...）
4. ToolMessage 返回结果
5. 下一个 LLM 调用看到这些 ToolMessage
```

---

## 11. API 层

### 11.1 Server

**文件**：`harness/api/server.py`

- 创建 FastAPI 应用
- 定义 `HarnessService` 基类接口
- 通过 `set_harness()` 注入具体实现

### 11.2 Routers

**文件**：`harness/api/routers.py`

| 路由 | 作用 |
|---|---|
| `POST /api/v1/execute` | 执行 agent |
| `POST /api/v1/clarification` | 回复澄清 |
| `POST /api/v1/threads/{thread_id}/stop` | 停止执行 |
| `GET /api/v1/threads/{thread_id}/status` | 查询状态 |
| `GET /api/v1/threads/{thread_id}/trace` | 查询 trace |
| `GET /api/v1/metrics/token-usage` | Token 使用统计 |

---

## 12. 关键文件速查

### 12.1 入口与 orchestration

| 文件 | 核心类/函数 | 作用 |
|---|---|---|
| `main.py` | `HarnessService` | 服务装配、执行、生命周期 |
| `graph_factory.py` | `build_harness_graph()` | 编译 Lead Agent 图 |
| `models.py` | `HarnessState`、`initial_state()` | 状态定义和初始状态 |

### 12.2 配置

| 文件 | 核心类 | 作用 |
|---|---|---|
| `config/__init__.py` | `HarnessConfig` | .env 配置 |
| `config/config_manager.py` | `ConfigManager` | YAML 热加载 |
| `config/memory_config.py` | `MemoryConfig` | 记忆配置 |
| `config/summarization_config.py` | `SummarizationConfig` | 摘要配置 |
| `config/checkpointer_config.py` | `CheckpointerConfig` | checkpoint 配置 |

### 12.3 Agent

| 文件 | 核心类 | 作用 |
|---|---|---|
| `agents/lead_agent.py` | `LeadAgent` | 系统提示词、工具构建 |
| `agents/subagent.py` | `SubAgent` | 子代理执行 |
| `agents/subagent_manager.py` | `SubagentManager` | 子代理并发调度 |
| `agents/features.py` | `RuntimeFeatures` | 特性开关 |

### 12.4 Middleware

| 文件 | 核心类 | 作用 |
|---|---|---|
| `middleware/__init__.py` | `AGENT_MIDDLEWARE_ORDER` | 注册顺序 |
| `middleware/base.py` | `HarnessAgentMiddleware` | 基类 |
| `middleware/dynamic_context.py` | `DynamicContextMiddleware` | 记忆/日期注入 |
| `middleware/summarization.py` | `SummarizationMiddleware` | 上下文压缩 |
| `middleware/memory.py` | `MemoryMiddleware` | 记忆更新入队 |
| `middleware/token_usage.py` | `TokenUsageMiddleware` | Token 统计 |
| `middleware/todo.py` | `TodoMiddleware` | Plan Mode |
| `middleware/title.py` | `TitleMiddleware` | 自动标题 |
| `middleware/llm_error.py` | `LLMErrorHandlingMiddleware` | LLM 错误重试 |
| `middleware/loop_detection.py` | `LoopDetectionMiddleware` | 循环检测 |
| `middleware/clarification.py` | `ClarificationMiddleware` | 澄清处理 |
| `middleware/subagent_limit.py` | `SubagentLimitMiddleware` | 子代理并发限制 |

### 12.5 Memory

| 文件 | 核心类/函数 | 作用 |
|---|---|---|
| `memory/storage.py` | `FileMemoryStorage` | 读写 memory.json |
| `memory/updater.py` | `MemoryUpdater` | LLM 驱动记忆更新 |
| `memory/queue.py` | `MemoryUpdateQueue` | debounce 更新队列 |
| `memory/prompt.py` | `format_memory_for_injection()` | 格式化记忆注入文本 |
| `memory/message_processing.py` | `filter_messages_for_memory()` | 消息过滤/信号检测 |
| `memory/summarization_hook.py` | `memory_flush_hook()` | 摘要前 flush 记忆 |

### 12.6 Runtime / Persistence

| 文件 | 核心类 | 作用 |
|---|---|---|
| `runtime/checkpointer/async_provider.py` | `AsyncCheckpointerProvider` | checkpoint saver 工厂 |
| `runtime/journal.py` | `RunJournal` | 运行事件日志 |
| `runtime/events/store/memory.py` | `InMemoryEventStore` | 事件内存存储 |
| `runtime/runs/store/memory.py` | `InMemoryRunStore` | run 元数据内存存储 |
| `persistence/engine.py` | `DatabaseEngine` | 数据库引擎 |

### 12.7 Tools

| 文件 | 核心类/函数 | 作用 |
|---|---|---|
| `tools/registry.py` | `ToolRegistry` | 工具注册与加载 |
| `tools/builtins/lead_tools.py` | `task`、`ask_clarification` | Lead Agent 核心工具 |
| `tools/sandbox_tools.py` | `bash`、`file_read` 等 | 沙箱/文件工具 |
| `tools/search.py` | `web_search`、`arxiv_search` | 搜索工具 |

### 12.8 Observability

| 文件 | 核心类 | 作用 |
|---|---|---|
| `observability/langfuse_manager.py` | `ObservabilityManager` | Langfuse trace、token 记录 |

### 12.9 工具函数

| 文件 | 核心函数 | 作用 |
|---|---|---|
| `utils.py` | `resolve_variable()` | 从 `module.path:variable` 路径加载对象 |

---

## 13. 常见修改路径

| 需求 | 修改位置 |
|---|---|
| 换模型 / 改 base_url | `harness/.env` |
| 开关记忆/摘要/标题 | `harness/config.yaml` + `RuntimeFeatures` |
| 改记忆提示词 | `harness/memory/prompt.py` |
| 新增 middleware | `harness/middleware/` + `AGENT_MIDDLEWARE_ORDER` |
| 新增工具 | `harness/tools/` + `config.yaml` tools 配置 |
| 改 checkpoint 后端 | `harness/config.yaml` checkpointer.backend |
| 改 API | `harness/api/routers.py` |

---

## 14. 注意事项

1. **Middleware 顺序很重要**：`AGENT_MIDDLEWARE_ORDER` 改变可能影响执行行为。
2. **State 中避免自定义 Pydantic 对象**：Checkpoint serde 可能无法序列化，优先用 dict 或 LangChain 标准类型。
3. **ConfigManager 热加载**：修改 `config.yaml` 后 3 秒内生效（部分配置需要重启）。
4. **Memory 更新是异步 debounce**：默认 30 秒，shutdown 时 `flush()`。
5. **SubAgent 独立运行**：不继承父 agent checkpoint，只通过 ToolMessage 返回结果。
