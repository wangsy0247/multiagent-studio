# SubAgent 实时会话窗口 — 实现方案

> 基于已完成的重构 (subagent-executor) 和前端增强 (detail-panel)，实现：
> 1. SubAgent 内部消息不泄漏到主聊天
> 2. 执行中的 SubAgent 可点击查看实时会话
> 3. 右侧面板展示完整子会话（思考 → 工具调用 → 结果 → 输出）

---

## 一、目标效果

```
主聊天 (左侧)                             子会话面板 (右侧, 点击 SubAgent 卡片后出现)
┌──────────────────────────┐       ┌──────────────────────────────────────┐
│ Lead: 让我帮你调查...      │       │ 🔍 researcher  ● 执行中  12.3s        │
│                          │       │ ─────────────────────────────────── │
│ ┌──────────────────┐     │       │ 💭 分析用户需求，需要多个信息源...     │
│ │🔍 researcher 执行中│ ←──┼───────│ 🔧 web_search("腾讯 Q3 财报")         │
│ │  5/60 turns      │ 点击 │       │ 📋 搜索结果: [5 items]              │
│ │  ████░░░░  33%   │     │       │ 💭 交叉验证中，需要更多数据...        │
│ └──────────────────┘     │       │ 🔧 web_fetch("https://...")        │
│                          │       │ 📋 页面内容: ...                    │
│ ┌──────────────────┐     │       │ 📤 输出: 腾讯股价下跌主要原因...      │
│ │💻 coder     完成   │     │       │ ✅ 完成 · 15 turns · 12.3k tokens  │
│ │  🔄10 turns🪙5.2k│     │       └──────────────────────────────────────┘
│ └──────────────────┘     │
│                          │
│ Lead: 综合分析结果...      │
└──────────────────────────┘
```

**核心原则**：
- 主聊天只放 Lead Agent 的消息 + SubAgent 紧凑进度卡片
- SubAgent 内部的 tool_call / tool_result / thinking **一律不出现**在主聊天
- 点击任意 SubAgent 卡片 → 右侧面板显示完整子会话
- 执行中可点击、完成后可点击，实时更新

---

## 二、后端改动

### 2.1 `subagent_executor.py` — 全消息收集 + 线程安全队列

**文件**：`harness/agents/subagent_executor.py`

**改动点 A**：`_aexecute()` 收集全部消息类型

```python
# 当前：只收集 AIMessage
ai_messages.append(messages[-1].model_dump())

# 改为：增量对比，收集所有新增消息
all_messages: list[dict[str, Any]] = []
last_msg_count = 0

async for chunk in agent.astream(state, config=run_config, stream_mode="values"):
    # 协作式取消检查 (保持)
    if cancel_event.is_set():
        ...
    
    final_state = chunk
    current_messages = chunk.get("messages", [])
    
    # 增量检测：找出上一次快照之后新增的消息
    new_msgs = current_messages[last_msg_count:]
    for msg in new_msgs:
        msg_dict = msg if isinstance(msg, dict) else msg.model_dump()
        all_messages.append(msg_dict)
        # 推送到主 loop 的队列 (实时广播)
        self._push_to_stream(msg_dict)
    
    last_msg_count = len(current_messages)
```

**改动点 B**：新增线程安全消息队列

```python
import queue  # stdlib 线程安全队列

# 模块级: subagent_name → asyncio.Queue
_subagent_streams: dict[str, "asyncio.Queue[dict]"] = {}
_subagent_streams_lock = threading.Lock()

def get_subagent_stream(name: str) -> "asyncio.Queue[dict]":
    """获取或创建 subagent 的消息队列 (主 event loop 消费)."""
    import asyncio
    with _subagent_streams_lock:
        if name not in _subagent_streams:
            _subagent_streams[name] = asyncio.Queue()
        return _subagent_streams[name]

def remove_subagent_stream(name: str) -> None:
    with _subagent_streams_lock:
        _subagent_streams.pop(name, None)
```

**改动点 C**：在 SubagentExecutor 中保存主 loop 引用并推消息

```python
class SubagentExecutor:
    def __init__(self, ...):
        ...
        # 保存主 event loop 引用 (构造时必须在主线程)
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None  # 测试环境, 不推实时消息
    
    def _push_to_stream(self, msg_dict: dict) -> None:
        """跨线程安全推送到主 event loop 的 asyncio.Queue."""
        if self._main_loop is None or self._main_loop.is_closed():
            return
        stream = get_subagent_stream(self.config.name)
        asyncio.run_coroutine_threadsafe(
            stream.put({
                "subagent_name": self.config.name,
                "trace_id": self.trace_id,
                "msg": msg_dict,
            }),
            self._main_loop,
        )
```

**改动点 D**：`SubagentResult` 包含全部消息

```python
# 当前返回
result_holder.ai_messages = ai_messages  # 只有 AIMessage

# 改为
result_holder.ai_messages = all_messages  # 全部消息 (含 ToolMessage)
```

---

### 2.2 SSE 路由 — 并行消费 subagent 队列

**文件**：`harness/api/routers.py` (或执行入口)

**改动点**：在 SSE `execute` 端点中，增加并行任务消费 subagent 队列

```python
async def _consume_subagent_streams(sse_writer):
    """并行消费所有活跃 subagent 的消息队列, 写入 SSE."""
    while True:
        # 获取当前活跃的队列
        with _subagent_streams_lock:
            active_names = list(_subagent_streams.keys())
        
        if not active_names:
            await asyncio.sleep(0.1)  # 没有活跃 subagent, 短暂休眠
            continue
        
        # 并发读取所有队列 (哪个先有数据用哪个)
        tasks = []
        for name in active_names:
            stream = _subagent_streams[name]
            tasks.append(_read_one(stream, name))
        
        done, _ = await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if result:
                await sse_writer(result)

async def _read_one(stream: asyncio.Queue, name: str) -> dict | None:
    """从单个 subagent 队列读一条消息 (非阻塞超时)."""
    try:
        item = await asyncio.wait_for(stream.get(), timeout=0.1)
        return _build_sse_event(item, name)
    except asyncio.TimeoutError:
        return None

def _build_sse_event(item: dict, name: str) -> dict:
    """将 subagent 消息转为 SSE 事件格式."""
    msg = item["msg"]
    msg_type = msg.get("type", "")
    
    if msg_type == "ai":
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            return {
                "type": "subagent_tool_call",
                "subagent_name": name,
                "subagent_task_id": item.get("trace_id"),
                "tool_name": tool_calls[0].get("name", "unknown"),
                "tool_args": tool_calls[0].get("args", {}),
            }
        content = msg.get("content", "")
        if content:
            return {
                "type": "subagent_thinking",
                "subagent_name": name,
                "subagent_task_id": item.get("trace_id"),
                "content": str(content),
            }
    
    elif msg_type == "tool":
        return {
            "type": "subagent_tool_result",
            "subagent_name": name,
            "subagent_task_id": item.get("trace_id"),
            "tool_result": str(msg.get("content", "")),
        }
    
    return None
```

**改动点**：在 execute 端点中启动并行消费

```python
# execute 端点 (简化)
async def execute_endpoint(...):
    sse_writer = SSEWriter(response)
    
    # 启动 subagent 队列消费协程 (并行)
    consumer_task = asyncio.create_task(_consume_subagent_streams(sse_writer))
    
    try:
        # 主流程: Lead Agent 执行
        async for event in graph.astream_events(...):
            sse_writer.send(event)
    finally:
        consumer_task.cancel()
```

---

### 2.3 `models.py` — 无需额外改动

已在之前重构中增强：`SubAgentResult.ai_messages: list[dict]` 可以容纳任意消息类型（AIMessage / ToolMessage 的 dict 形式）。

---

## 三、前端改动

### 3.1 `types.ts` — 新增 SSE 事件类型

**文件**：`frontend/src/lib/types.ts`

```typescript
export type SSEEventType =
  | ...
  | "subagent_thinking"      // SubAgent 内部思考过程
  | "subagent_tool_call"     // SubAgent 内部工具调用
  | "subagent_tool_result";  // SubAgent 内部工具结果

export interface SSEEvent {
  ...
  // 子会话路由字段 (所有 subagent_* 事件携带)
  subagent_task_id?: string;

  // subagent_tool_call 专用
  subagent_tool_name?: string;
  subagent_tool_args?: Record<string, unknown>;

  // subagent_tool_result 专用
  subagent_tool_result?: string;

  // subagent_thinking 专用
  subagent_content?: string;
}
```

### 3.2 `chat-store.ts` — 子会话存储 + 消息分流

**文件**：`frontend/src/lib/chat-store.ts`

**改动点 A**：新增状态

```typescript
interface ChatStore {
  // ... 现有字段 ...

  /** SubAgent 子会话: subagent_name → 消息列表 */
  subConversations: Record<string, ChatMessage[]>;

  /** 追加消息到指定 SubAgent 的子会话 */
  appendToSubConversation: (name: string, msg: Omit<ChatMessage, "id" | "createdAt">) => void;
}
```

**改动点 B**：事件分流逻辑

```typescript
case "subagent_start":
  // 初始化子会话存储
  if (event.subagent_name && !s.subConversations[event.subagent_name]) {
    set((s) => ({
      subConversations: { ...s.subConversations, [event.subagent_name]: [] }
    }));
  }
  // 主聊天仍显示进度卡片
  get().addMessage({ role: "subagent", msgType: "subagent_start", ... });
  break;

case "subagent_progress":
  // 更新进度卡片的 metadata (保持)
  break;

case "subagent_end":
  // 主聊天: 结果卡片
  get().addMessage({ role: "subagent", msgType: "subagent_end", ... });
  // 子会话: 最终输出
  if (event.subagent_name && event.subagent_result) {
    get().appendToSubConversation(event.subagent_name, {
      role: "subagent",
      content: event.subagent_result.output || "",
      msgType: "subagent_output",
      metadata: { status: event.subagent_result.status, ... },
      tokenCount: totalTokens,
    });
  }
  break;

// 新增: SubAgent 内部事件 → 只进子会话, 不进主聊天
case "subagent_thinking":
  if (event.subagent_name && event.subagent_content) {
    get().appendToSubConversation(event.subagent_name, {
      role: "ai",
      content: event.subagent_content,
      msgType: "thinking",
      metadata: {},
      tokenCount: 0,
    });
  }
  break;

case "subagent_tool_call":
  if (event.subagent_name) {
    get().appendToSubConversation(event.subagent_name, {
      role: "tool",
      content: event.subagent_tool_name || "unknown",
      msgType: "tool_call",
      metadata: {
        tool_name: event.subagent_tool_name,
        tool_args: event.subagent_tool_args,
      },
      tokenCount: 0,
    });
  }
  break;

case "subagent_tool_result":
  if (event.subagent_name) {
    get().appendToSubConversation(event.subagent_name, {
      role: "tool",
      content: event.subagent_tool_result || "",
      msgType: "tool_result",
      metadata: { tool_result: event.subagent_tool_result },
      tokenCount: 0,
    });
  }
  break;
```

**⚠️ 关键**: 上述 `subagent_*` 事件 **不调用** `get().addMessage()`（只调用 `appendToSubConversation`），因此不会出现在主聊天的 `messages[]` 中。

**改动点 C**：切线程时清空 + ThreadMessage 持久化

```typescript
setActiveThread: (threadId) => {
  // ... 现有逻辑 ...
  set({
    ...
    subConversations: {},  // 清空子会话
  });
}
```

### 3.3 `SubagentDetailPanel.tsx` — 实时渲染子会话

**文件**：`frontend/src/components/chat/SubagentDetailPanel.tsx`

**改动点 A**：同时显示 SubAgent 的内部消息

```tsx
export default function SubagentDetailPanel() {
  const {
    messages,
    subConversations,
    selectedSubagentId,
    selectSubagent,
  } = useChatStore();

  const targetMsg = messages.find(m => m.id === selectedSubagentId);
  const name = targetMsg?.metadata?.subagent_name as string;
  const result = targetMsg?.metadata?.subagent_result as SubAgentResultData | undefined;

  // 从子会话存储中读取实时消息
  const liveMessages = name ? (subConversations[name] || []) : [];

  return (
    <aside>
      {/* Header: name + status + duration + close */}
      
      {/* 如果有实时消息, 优先展示 (更丰富) */}
      {liveMessages.length > 0 ? (
        <SubConversationTimeline messages={liveMessages} />
      ) : (
        /* 回退: 展示 ai_messages (旧数据) */
        <StaticReasoning aiMessages={result?.ai_messages || []} />
      )}

      {/* 底部: 统计 + token 明细 + 时序 */}
    </aside>
  );
}
```

**改动点 B**：`SubConversationTimeline` 子组件

```tsx
function SubConversationTimeline({ messages }: { messages: ChatMessage[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // 自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  return (
    <div className="space-y-2">
      {messages.map((msg, i) => {
        switch (msg.msgType) {
          case "thinking":
            return <ThinkingBubble key={i} content={msg.content} />;
          case "tool_call":
            return <ToolCallBubble key={i} name={msg.metadata.tool_name} args={msg.metadata.tool_args} />;
          case "tool_result":
            return <ToolResultBubble key={i} content={msg.content} />;
          case "subagent_output":
            return <OutputBubble key={i} content={msg.content} />;
          default:
            return null;
        }
      })}
      <div ref={bottomRef} />
    </div>
  );
}

function ThinkingBubble({ content }: { content: string }) {
  return (
    <div className="flex items-start gap-2 p-2.5 bg-purple-50 border border-purple-100 rounded-lg">
      <Brain className="w-3.5 h-3.5 text-purple-400 mt-0.5 flex-shrink-0" />
      <p className="text-xs text-slate-600 leading-relaxed">{content}</p>
    </div>
  );
}

function ToolCallBubble({ name, args }: { name: string; args?: Record<string, unknown> }) {
  return (
    <div className="flex items-start gap-2 p-2.5 bg-amber-50 border border-amber-100 rounded-lg">
      <Wrench className="w-3.5 h-3.5 text-amber-500 mt-0.5 flex-shrink-0" />
      <div>
        <span className="text-xs font-medium text-amber-700">🔧 {name}</span>
        {args && (
          <pre className="text-[10px] text-amber-600 mt-1 font-mono">
            {JSON.stringify(args, null, 1).slice(0, 300)}
          </pre>
        )}
      </div>
    </div>
  );
}

function ToolResultBubble({ content }: { content: string }) {
  return (
    <div className="flex items-start gap-2 p-2.5 bg-green-50 border border-green-100 rounded-lg">
      <CheckCircle className="w-3.5 h-3.5 text-green-400 mt-0.5 flex-shrink-0" />
      <p className="text-[10px] text-slate-600 leading-relaxed font-mono line-clamp-4">
        {content}
      </p>
    </div>
  );
}

function OutputBubble({ content }: { content: string }) {
  return (
    <div className="p-3 bg-white border-2 border-emerald-300 rounded-lg">
      <p className="text-[10px] text-emerald-500 uppercase tracking-wider mb-1">📤 最终输出</p>
      <ReactMarkdown remarkPlugins={[remarkGfm]} className="prose prose-sm max-w-none text-sm">
        {content}
      </ReactMarkdown>
    </div>
  );
}
```

### 3.4 `SubAgentCard.tsx` — 放开执行中点击

**文件**：`frontend/src/components/chat/SubAgentCard.tsx`

```typescript
// 当前
const handleClick = () => {
  if (isEnd) {  // ← 限制: 只有完成才能点击
    selectSubagent(isSelected ? null : message.id);
  }
};

// 改为: 任何状态可点击
const handleClick = () => {
  selectSubagent(isSelected ? null : message.id);
};

// 样式: 执行中/进度中卡片也显示 cursor-pointer + hover 效果
className={cn(
  ...
  "cursor-pointer hover:shadow-md hover:border-slate-300 active:scale-[0.99]",
  isSelected && "ring-2 ring-slate-400 shadow-md",
)}
```

---

## 四、关键类型定义 (快查)

```python
# ── 后端新增 ──
# subagent_executor.py

# 模块级队列字典
_subagent_streams: dict[str, asyncio.Queue[dict]]  # subagent_name → 消息队列

# SubagentExecutor 新增属性
self._main_loop: asyncio.AbstractEventLoop | None   # 主 event loop 引用

# SubagentExecutor._aexecute() 增量收集
all_messages: list[dict[str, Any]]  # 全部消息 (AIMessage + ToolMessage)
```

```typescript
// ── 前端新增 ──
// types.ts

type SSEEventType |= "subagent_thinking" | "subagent_tool_call" | "subagent_tool_result";

interface SSEEvent {
  subagent_task_id?: string;
  subagent_tool_name?: string;
  subagent_tool_args?: Record<string, unknown>;
  subagent_tool_result?: string;
  subagent_content?: string;
}

// chat-store.ts
interface ChatStore {
  subConversations: Record<string, ChatMessage[]>;
  appendToSubConversation: (name: string, msg) => void;
}
```

---

## 五、实施顺序

| 步骤 | 文件 | 验证方式 |
|---|---|---|
| **1** | `subagent_executor.py`: 全消息收集 + 增量检测 | 检查 `SubAgentResult.ai_messages` 是否包含 ToolMessage |
| **2** | `subagent_executor.py`: 线程安全队列 + `_push_to_stream` | 单元测试: 跨线程 `run_coroutine_threadsafe` |
| **3** | `routers.py`: SSE 端点并行消费 subagent 队列 | `curl -N` 观察 SSE 中是否出现 `subagent_tool_call` |
| **4** | `types.ts` + `chat-store.ts`: 子会话存储 + 事件分流 | zustand devtools: 主 `messages` 中无 SubAgent 工具调用 |
| **5** | `SubagentDetailPanel.tsx`: `SubConversationTimeline` 实时渲染 | 点击执行中 SubAgent → 看到实时工具调用 |
| **6** | `SubAgentCard.tsx`: 放开点击限制 | 进度卡片可点击 |
| **7** | 集成测试 + 回归 | 并发 3 SubAgent, 主聊天清洁, 子会话完整 |

---

## 六、边界与风险

| 风险 | 缓解 |
|---|---|
| `get_running_loop()` 在测试环境抛异常 | `try/except RuntimeError` → `_main_loop = None`, 不推实时消息 |
| 并发 SubAgent 过多导致 asyncio.Queue 堆积 | 子会话面板显示最新 N 条消息（虚拟滚动）|
| SSE 断开后 subagent 队列泄漏 | `try/finally` → `remove_subagent_stream(name)` 清理 |
| 旧前端不识别新 SSE 事件类型 | 新增事件类型在 `handleSSEEvent` 中 fallthrough → 主聊天无影响 |
| `stream_mode="values"` 快照中的 messages 可能被中间件修改 | 增量检测基于同一个 state 的 messages 数组，不受中间件插入影响 |
