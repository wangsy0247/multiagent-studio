# 用户请求完整生命周期

从用户在前端发送一条消息，到 AI 回复渲染完成，整个调用链路涉及三个进程、四层架构。本文档从函数层面逐一说明。

## 架构总览

```
┌──────────────┐     HTTP SSE      ┌──────────────────┐     HTTP SSE      ┌──────────────────┐
│  前端浏览器    │ ◄─────────────── │  App 服务 (:8000)  │ ◄─────────────── │ Harness 服务      │
│  Next.js     │                   │  FastAPI          │                   │ (:8001)          │
│              │                   │  PostgreSQL       │                   │ LangGraph         │
└──────────────┘                   └──────────────────┘                   └──────────────────┘
    ▲                                    ▲  ▲                                    ▲  ▲
    │                                    │  │                                    │  │
    │ 用户点击发送                         │  │ JWT 鉴权                           │  │ 三层配置合并
    │ ChatUI 渲染 SSE                     │  │ 持久化 Message                      │  │ LangGraph ReAct 循环
    │                                    │  │ 转发 SSE                           │  │ 20 层中间件链
    ▼                                    ▼  ▼                                    ▼  ▼
```

**三个进程：**
1. **前端** (Next.js, 端口 3000) — Chat UI，消费 SSE 流渲染消息
2. **App 服务** (FastAPI, 端口 8000) — 鉴权、持久化、代理 Harness SSE
3. **Harness 服务** (FastAPI, 端口 8001) — Agent 执行引擎，LangGraph 图运行

---

## 第 1 层：前端 → App 服务

### 入口：`app/api/execute.py:43` — `execute()`

用户点击发送 → 前端 POST 到 `/api/v1/execute`：

```json
{
  "thread_id": "uuid",
  "message": "你好",
  "agent_name": "default",
  "mode": "single"
}
```

```python
# app/api/execute.py:43
@router.post("")
async def execute(req: ExecuteRequest, current_user: User = Depends(get_current_user)):
```

**关键步骤：**
1. **JWT 鉴权** — `get_current_user` 从 `Authorization: Bearer <token>` 提取 user_id
2. **更新线程状态** — `Thread.status = "running"`，写入 PostgreSQL
3. **创建 SSE 生成器** — `event_generator()` 是 async generator，逐条转发 Harness 事件
4. **返回 StreamingResponse** — `media_type="text/event-stream"`，浏览器以 SSE 协议接收

### SSE 代理循环：`app/api/execute.py:65` — `event_generator()`

```python
async def event_generator():
    async for event_json in harness.stream_execute(
        thread_id=req.thread_id,
        user_id=str(current_user.id),
        message=req.message,
        agent_name=req.agent_name,
        mode=req.mode,
    ):
        event = json.loads(event_json)

        # 持久化关键事件到 PostgreSQL
        if event_type in ("tool_call", "tool_result", "subagent_start", ...):
            msg = Message(thread_id=..., content=..., msg_type=...)
            db.add(msg); await db.commit()

        # 转发 SSE 到前端
        yield f"data: {json.dumps(event)}\n\n"

        # 处理终止事件
        if event_type == "finished":
            thread.status = "finished"
        elif event_type == "error":
            thread.status = "error"
```

**关键点：**
- 每收到一个 Harness SSE 事件 → 立刻 `yield` 给前端（低延迟流式传输）
- `tool_call` / `tool_result` / `subagent_start` 等事件同步持久化到 `messages` 表
- 流式 token 在 `finished` 时批量写入（累积 `_accumulated_ai_text`）
- `title_update` 事件自动回写 `threads.title` 字段

---

## 第 2 层：App 服务 → Harness 服务 (HTTP 代理)

### 代理客户端：`app/services/harness_client.py:40` — `stream_execute()`

```python
class HarnessClient:
    async def stream_execute(self, thread_id, user_id, message, ...):
        payload = {
            "thread_id": thread_id,
            "user_id": user_id,
            "message": message,
            "agent_name": agent_name or "default",
            "mode": mode,
        }

        async with client.stream("POST", f"{self.base_url}/api/v1/execute", json=payload) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line[6:]  # 去 "data: " 前缀
```

**关键点：**
- 使用 `httpx.AsyncClient.stream()` — 支持 SSE 长连接
- `timeout=httpx.Timeout(None)` — 无超时限制，LLM 调用可能很长
- 单例模式 — `get_harness_client()` 全局复用同一个连接池

---

## 第 3 层：Harness 服务 — 核心引擎

### 入口：`harness/api/routers.py:29` — `execute()`

```python
@router.post("/execute")
async def execute(request: ExecuteRequest, harness: HarnessService = Depends(get_harness)):
    async def event_stream():
        async for event in harness.execute(
            thread_id=request.thread_id,
            user_id=request.user_id,
            message=request.message,
            agent_name=request.agent_name,
            mode=request.mode,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 核心执行：`harness/main.py:690` — `HarnessService.execute()`

```python
async def execute(self, thread_id, user_id, message, ..., agent_name="default"):
```

#### 阶段 A：懒初始化 (仅首次调用)

```python
if not self._initialized:
    await self.initialize(agent_name=agent_name, user_id=user_id)
```

`initialize()` 内部步骤：

| 步骤 | 函数 | 作用 |
|------|------|------|
| A1 | `_bootstrap_check(user_id)` | 检查 L1 `api_key` 是否配置，缺失则 warn |
| A2 | `_ensure_default_agent(user_id)` | 幂等创建 default agent |
| A3 | `ConfigLoader.load_effective(user_id, agent_name)` | **三层配置合并** → `EffectiveConfig` |
| A4 | `_init_llm(model, api_key, base_url, ...)` | 创建 `ChatOpenAI` 实例 |
| A5 | `tool_registry.load_tools_from_config()` | 从 YAML 加载工具定义 |
| A6 | `tool_registry.load_mcp_tools()` | 从 `extensions_config.json` 加载 MCP 工具 |
| A7 | `set_memory_config(MemoryConfig(...))` | 初始化记忆系统 (MemoryQueue, mem0) |
| A8 | `ObservabilityManager(cfg)` | 初始化 Langfuse tracing |
| A9 | `_register_middlewares()` | 构建 20 层中间件链 |
| A10 | `LeadAgent(...)` | 构建系统提示词 + agent 工具 |
| A11 | `AsyncCheckpointerProvider.get_checkpointer()` | 连接 SQLite/Postgres checkpointer |
| A12 | `build_harness_graph(...)` | LangGraph 编译 |

**A3 详解 — 三层配置合并：**

```
L0: SYSTEM_DEFAULTS (defaults.py)
    tool_groups=["search","files","files_readonly","mcp"]
    memory={backend:"file", max_facts:10, ...}
    summarization={enabled:true, ...}
    ...

  + L1: ~/.multiagent-studio/users/{uid}/config.yaml
    api_key: "sk-xxx"
    base_url: "https://..."
    default_model: "qwen3.6-plus"
    summary_model: ""
    memory_model: ""
    memory: {max_injection_tokens: 500, ...}
    ...

  + L2: ~/.multiagent-studio/users/{uid}/agents/{name}/config.yaml
    model: "gpt-4o"
    temperature: 0.3
    tool_groups: ["files"]       ← 扩展到 L0
    memory: {max_facts: 20}      ← 覆盖 L0
    ...

  + HARDCODED_OVERRIDES
    loop_detection.enabled = true  (不可配置)
    worktree.enabled = true        (不可配置)

  = EffectiveConfig
    model="gpt-4o"
    api_key="sk-xxx"
    base_url="https://..."
    tool_groups=["search","files","files_readonly","mcp"]  ← L0+L2 合并
    memory_max_facts=20          ← L2 覆盖 L0
    loop_detection_enabled=True  ← HARDCODED_OVERRIDES 强制
    ... (共 60+ 扁平字段)
```

**A9 详解 — 中间件链 (harness/agents/lead_agent.py:714)：**

```python
def _build_middlewares(config: RunnableConfig):
    middlewares = [
        ThreadDataMiddleware,         # 注入 workspace 路径
        UploadsMiddleware,            # 上传文件处理
        SandboxMiddleware,            # 沙箱隔离
        DanglingToolCallMiddleware,   # 悬空 tool_call 修复
        LLMErrorHandlingMiddleware,   # LLM 错误重试
        GuardrailMiddleware,          # (可选) 安全护栏
        SandboxAuditMiddleware,       # 沙箱审计追踪
        ToolErrorHandlingMiddleware,  # Tool 错误重试
        DynamicContextMiddleware,     # 注入日期 + 记忆到 system prompt
        SummarizationMiddleware,      # (可选) 长上下文压缩
        TodoMiddleware,               # (可选) Plan 模式 TODO
        TokenUsageMiddleware,         # Token 统计
        TitleMiddleware,              # (可选) 自动生成标题
        MemoryMiddleware,             # (可选) 记忆提取队列
        ViewImageMiddleware,          # (可选) 图片预览
        DeferredToolFilterMiddleware, # (可选) Tool 延迟过滤
        SubagentLimitMiddleware,      # SubAgent 并发限制
        LoopDetectionMiddleware,      # 循环检测 + 熔断
        SafetyFinishReasonMiddleware, # finish_reason 安全检查
        ClarificationMiddleware,      # 澄清请求处理
    ]
```

#### 阶段 B：状态恢复

```python
# B1: 从 checkpointer 恢复历史 state
state_snapshot = await self.graph.aget_state(build_config)

if state_snapshot is not None and state_snapshot.values:
    # 已有会话 → 追加新消息
    current_state = dict(state_snapshot.values)
    current_state["messages"] = current_state["messages"] + [
        HumanMessage(content=message)
    ]
else:
    # 新会话 → 初始化 state
    current_state = initial_state(thread_id, user_id, message, files)

# B4: 绑定 Langfuse callback + run_id
build_config = self._build_config(thread_id)
# B5: 绑定 RunJournal (事件持久化)
journal = RunJournal(thread_id, run_id, user_id, self._event_store)
build_config["callbacks"] = [*existing_callbacks, journal]
```

#### 阶段 C：图执行 (流式)

```python
# Emit start event
yield {"type": "message", "status": "started", "thread_id": thread_id}

# 流式执行 LangGraph
async for mode, chunk in runner.astream(
    current_state,
    build_config,
    stream_mode=["messages", "updates", "custom"],
):
    # 根据 chunk 类型构造 SSE 事件
    if isinstance(chunk, AIMessageChunk):
        yield {"type": "message", "content": chunk.content, ...}
    elif ...
```

---

## 第 4 层：LangGraph 图执行

### 图结构：`harness/graph_factory.py:97` — `build_harness_graph()`

```python
def build_harness_graph(llm, tools, middlewares, system_prompt, checkpointer):
    inner_agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,   # SOUL.md + 能力描述
        middleware=middlewares,
        state_schema=HarnessState,
    )

    graph = StateGraph(HarnessState)
    graph.add_node("agent", inner_agent)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)

    return graph.compile(checkpointer=checkpointer)
```

**架构：**
```
StateGraph:
  START → agent (create_agent 子图) → END
```

无显式的 memory_update / clarify 节点 — 这些功能全部由中间件在钩子中驱动。

### ReAct 循环 (LangChain `create_agent` 内部)

```
┌─────────────────────────────────────────────────┐
│  循环:                                           │
│                                                  │
│  1. before_model 钩子                             │
│     ├─ DynamicContextMiddleware                  │
│     │    → 注入当前日期、用户记忆到 system prompt    │
│     ├─ ThreadDataMiddleware                      │
│     │    → 注入 workspace_root 路径               │
│     ├─ UploadsMiddleware                         │
│     │    → 注入上传文件列表                        │
│     └─ LoopDetectionMiddleware                   │
│          → 递增计数器，检测 loop                   │
│                                                  │
│  2. LLM 调用                                     │
│     ChatOpenAI.ainvoke(messages)                 │
│     → 流式返回 token (打字机效果)                   │
│                                                  │
│  3. after_model 钩子                              │
│     ├─ TitleMiddleware                           │
│     │    → 首条回复后生成标题 (qwen3.6-plus 等)     │
│     ├─ SummarizationMiddleware                   │
│     │    → token 超阈值时压缩上下文                 │
│     ├─ MemoryMiddleware                          │
│     │    → 队列化记忆更新 (debounce 120s)          │
│     ├─ TokenUsageMiddleware                      │
│     │    → 统计 token 用量                        │
│     └─ ClarificationMiddleware                   │
│          → 检测是否需要向用户澄清                   │
│                                                  │
│  4. 如果有 tool_calls:                            │
│     ├─ before_tool / after_tool 钩子              │
│     │   └─ SandboxMiddleware                     │
│     │        → 沙箱隔离文件操作                    │
│     ├─ tool 执行 → tool_result                    │
│     └─ 回到步骤 1                                 │
│                                                  │
│  5. 没有 tool_calls: 退出循环                      │
│     → 返回 final AI response                     │
└─────────────────────────────────────────────────┘
```

---

## SSE 事件类型

| type | 含义 | 触发时机 |
|------|------|---------|
| `message` | AI 回复内容 | LLM 流式输出 token |
| `tool_call` | 工具调用 | Agent 决定使用工具 |
| `tool_result` | 工具结果 | 工具执行完成 |
| `subagent_start` | 子 Agent 启动 | Task 工具派发 |
| `subagent_end` | 子 Agent 完成 | Task 执行结束 |
| `thinking` | 思考过程 | 模型开启 thinking 时 |
| `token_usage` | Token 统计 | TokenUsageMiddleware |
| `title_update` | 标题生成 | TitleMiddleware |
| `clarification` | 需要澄清 | ClarificationMiddleware |
| `loop_warning` | 循环警告 | LoopDetectionMiddleware |
| `finished` | 执行完成 | graph.astream 结束 |
| `error` | 执行错误 | 异常捕获 |

---

## 记忆系统生命周期

```
对话结束
  │
  ├─ MemoryMiddleware.after_model → 提取最近消息
  ├─ MemoryQueue.enqueue(thread_id, messages, agent_name, user_id)
  │     ↓ debounce 120s (可配置)
  ├─ MemoryUpdater.aupdate_memory()
  │     ├─ backend=file: LLM 提取事实 → JSON 写入 ~/.multiagent-studio/memory/
  │     └─ backend=mem0: mem0.add() → Chroma 向量存储
  │
下次对话
  │
  ├─ DynamicContextMiddleware.before_model
  │     ├─ 从 MemoryStorage 加载记忆
  │     └─ 注入到 system prompt: "[用户记忆] ..."
  └─ LLM 调用时携带记忆上下文
```

---

## 错误处理

```
App 层异常
  ├─ HarnessUnavailableError (httpx.ConnectError)
  │   → thread.status = "error"
  │   → yield {type:"error", content:"Harness 服务不可用"}
  │
  ├─ HTTPException (404, 401)
  │   → FastAPI 自动返回 JSON error
  │
  └─ 其他 Exception
      → logger.exception()
      → thread.status = "error"
      → yield {type:"error", content:str(e)}

Harness 层异常
  ├─ Agent 不存在
  │   → yield {type:"error", content:"Agent 'xxx' 不存在"}
  │
  ├─ LLM API 错误 (ChatOpenAI)
  │   → LLMErrorHandlingMiddleware 重试
  │   → 最终失败 → yield {type:"error"}
  │
  └─ Tool 执行错误
      → ToolErrorHandlingMiddleware 重试
      → 最终失败 → tool_result = error message
```

---

## 关键单例 / 全局状态

| 对象 | 创建位置 | 生命周期 |
|------|---------|---------|
| `HarnessClient` | `app/services/harness_client.py:161` | App 进程级别 |
| `HarnessService` | `harness/api/server.py:get_harness()` | Harness 进程级别 |
| `_effective_config` | `HarnessService._effective_config` | 首次 initialize 时设置 |
| `MemoryQueue` | `harness/memory/queue.py:get_memory_queue()` | 进程级别 singleton |
| `MemoryConfig` | `harness/config/memory_config.py:set_memory_config()` | 进程级别 global |
| `ToolRegistry` | `HarnessService.tool_registry` | HarnessService 实例级别 |
| `ObservabilityManager` | `HarnessService.observability` | HarnessService 实例级别 |
