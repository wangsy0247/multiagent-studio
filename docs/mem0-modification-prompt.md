# mem0 接入修改提示词——每轮都查都注入方案

> 目标：用 mem0 + Chroma 替换现有 FileMemoryStorage，采用"每轮都查都注入"方案。
> 核心原则：每轮 abefore_agent 都用最新消息 search mem0，用 RemoveMessage 删旧 reminder + 追加新 reminder，不做相似度判断。

---

## 一、修改目标与背景

### 当前架构
```
DynamicContextMiddleware (abefore_agent) → get_memory_data() → 读 JSON → 首回合注入后冻结
MemoryMiddleware (aafter_agent) → queue → MemoryUpdater → LLM 提取 → 写 JSON
```

### 目标架构
```
DynamicContextMiddleware (abefore_agent) → mem0.search() → 每轮注入（RemoveMessage 替换旧 reminder）
MemoryMiddleware (aafter_agent) → queue → mem0.add()（内部含 LLM 提取+冲突检测）
```

### 方案特点
- **每轮都查**：abefore_agent 每次都用最新用户消息调 `mem0.search()`（50-100ms，无 LLM 调用）
- **每轮都注入**：用 LangGraph 的 `RemoveMessage` 删除上一轮的 reminder，追加新 reminder（无累积）
- **组合查询**：首回合用"固定查询+首条消息"两次 search；后续回合只用最新消息 search
- **不做相似度判断**：去掉 embedding API 调用和 cosine 相似度计算，代码最简
- **时间感知**：写入时通过 metadata 传时间；检索时用 created_at 的 gte/lte 过滤

---

## 二、涉及的文件清单

| 文件 | 操作 | 改动说明 |
|------|------|---------|
| `requirements.txt` 或 `pyproject.toml` | 修改 | 新增 `mem0ai`、`chromadb` 依赖 |
| `harness/config/memory_config.py` | 修改 | 新增 mem0 相关配置字段 |
| `harness/config.yaml` | 修改 | 新增 mem0_config 配置段 |
| `harness/memory/mem0_client.py` | **新增** | mem0 客户端单例，初始化 Memory 实例 |
| `harness/middleware/dynamic_context.py` | **重构** | 改为每轮 search + RemoveMessage 替换 |
| `harness/middleware/memory.py` | 修改 | queue 的 messages 传给 mem0.add() |
| `harness/memory/updater.py` | **简化** | aupdate_memory 改为调 mem0.add() |
| `harness/memory/queue.py` | 小改 | ConversationContext 加 metadata 字段 |
| `scripts/migrate_memory_to_mem0.py` | **新增** | 一次性数据迁移脚本 |

---

## 三、详细修改指令

### 3.1 新增依赖

**文件**：`requirements.txt`（或 `pyproject.toml`）

新增：
```
mem0ai>=0.1.0
chromadb>=0.5.0
```

### 3.2 修改配置

**文件**：`harness/config/memory_config.py`

在 `MemoryConfig` 类中新增以下字段：

```python
class MemoryConfig(BaseModel):
    # ... 现有字段保持不变 ...
    
    # ── mem0 配置（新增）──
    backend: str = Field(
        default="file",
        description="Memory backend: 'file' (legacy JSON) or 'mem0' (mem0+vector store)",
    )
    mem0_config: dict = Field(
        default_factory=dict,
        description="mem0 configuration dict, see mem0 docs. Only used when backend='mem0'",
    )
    mem0_search_top_k: int = Field(
        default=5, ge=1, le=20,
        description="Number of memories to retrieve per search",
    )
    mem0_general_query: str = Field(
        default="用户的偏好、习惯、背景和重要信息",
        description="Fixed query for retrieving general user memories on first turn",
    )
    mem0_enable_time_filter: bool = Field(
        default=False,
        description="Whether to filter memories by created_at recency",
    )
    mem0_recent_days: int = Field(
        default=90, ge=1, le=365,
        description="Only retrieve memories created within this many days (when time filter enabled)",
    )
```

**文件**：`harness/config.yaml`

修改 memory 段：

```yaml
memory:
  enabled: True
  backend: mem0              # file | mem0
  debounce_seconds: 120
  # 以下 file backend 的配置保留（向后兼容）
  max_facts: 100
  fact_confidence_threshold: 0.8
  injection_enabled: true
  max_injection_tokens: 1000
  # mem0 专属配置
  mem0_search_top_k: 5
  mem0_general_query: "用户的偏好、习惯、背景和重要信息"
  mem0_enable_time_filter: false
  mem0_recent_days: 90
  mem0_config:
    vector_store:
      provider: chroma
      config:
        collection_name: memories
        path: ~/.multiagent-studio/chroma   # 本地文件存储
    llm:
      provider: openai
      config:
        model: qwen-plus                    # 复用项目已有模型
        api_base: ${OPENAI_BASE_URL}        # 复用项目的 OpenAI 兼容端点
        api_key: ${OPENAI_API_KEY}
    embedder:
      provider: openai
      config:
        model: text-embedding-3-small
        api_base: ${OPENAI_BASE_URL}
        api_key: ${OPENAI_API_KEY}
```

### 3.3 新增 mem0 客户端

**文件**：`harness/memory/mem0_client.py`（新增）

```python
"""mem0 client singleton — initialized once from config.

Usage:
    from harness.memory.mem0_client import get_mem0, is_mem0_enabled
    if is_mem0_enabled():
        mem0 = get_mem0()
        results = mem0.search(query, filters={"user_id": uid, "agent_id": aid})
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_mem0_instance: Any | None = None  # mem0.Memory instance
_initialized: bool = False


def is_mem0_enabled() -> bool:
    """Check if mem0 backend is enabled."""
    from harness.config.memory_config import get_memory_config
    cfg = get_memory_config()
    return cfg.enabled and cfg.backend == "mem0"


def get_mem0() -> Any:
    """Get the singleton mem0.Memory instance.

    Lazily initialized on first call. Returns None if mem0 is not enabled
    or initialization failed.
    """
    global _mem0_instance, _initialized
    if _initialized:
        return _mem0_instance
    
    from harness.config.memory_config import get_memory_config
    cfg = get_memory_config()
    
    if not cfg.enabled or cfg.backend != "mem0":
        _initialized = True
        return None
    
    if not cfg.mem0_config:
        logger.error("mem0 backend enabled but mem0_config is empty")
        _initialized = True
        return None
    
    try:
        from mem0 import Memory
        _mem0_instance = Memory.from_config(cfg.mem0_config)
        logger.info("mem0 client initialized with config: %s", 
                     {k: v for k, v in cfg.mem0_config.items() if k != "llm"})
    except ImportError:
        logger.error("mem0ai not installed. Run: pip install mem0ai chromadb")
    except Exception as e:
        logger.error("Failed to initialize mem0: %s", e)
    
    _initialized = True
    return _mem0_instance


def reset_mem0() -> None:
    """Reset the singleton (for testing)."""
    global _mem0_instance, _initialized
    _mem0_instance = None
    _initialized = False
```

### 3.4 重构 DynamicContextMiddleware（核心改动）

**文件**：`harness/middleware/dynamic_context.py`

**改动要点**：
1. 每轮 `abefore_agent` 都调 `mem0.search()`（不是首回合才查）
2. 首回合用组合查询（固定查询 + 首条消息）；后续回合只用最新消息
3. 用 `RemoveMessage` 删除上一轮的 reminder，追加新 reminder（无累积）
4. 保留日期注入逻辑（跨午夜更新日期）

**完整重构后的代码**：

```python
"""DynamicContextMiddleware — inject memory and current date as a <system-reminder>.

每轮都查都注入方案：
- 每轮 abefore_agent 用最新用户消息调 mem0.search()
- 首回合用组合查询（固定查询 + 首条消息）
- 用 RemoveMessage 删除上一轮的 reminder，追加新 reminder（无累积）
- 保留日期注入逻辑（跨午夜更新日期）

Reminder format:
    <system-reminder>
    <memory>...</memory>
    <current_date>2026-07-01, Tuesday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, UTC
from typing import override

from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.config.memory_config import get_memory_config
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"<current_date>([^<]+)</current_date>")
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"
_SUMMARY_MESSAGE_NAME = "summary"


def _extract_date(content: str) -> str | None:
    m = _DATE_RE.search(content)
    return m.group(1) if m else None


def is_dynamic_context_reminder(message: object) -> bool:
    return isinstance(message, HumanMessage) and bool(
        message.additional_kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY)
    )


def _last_injected_date(messages: list) -> str | None:
    for msg in reversed(messages):
        if is_dynamic_context_reminder(msg):
            content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
            return _extract_date(content_str)
    return None


def _is_user_injection_target(message: object) -> bool:
    return (
        isinstance(message, HumanMessage)
        and not is_dynamic_context_reminder(message)
        and message.name != _SUMMARY_MESSAGE_NAME
    )


class DynamicContextMiddleware(HarnessAgentMiddleware):
    """每轮都查都注入：每轮用最新消息 search mem0，RemoveMessage 替换旧 reminder。"""

    name = "dynamic_context"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None):
        super().__init__(config)
        self._agent_name = agent_name

    # ── mem0 检索 ────────────────────────────────────────────────────────

    def _search_mem0(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """同步调用 mem0.search()，返回记忆列表。"""
        from harness.memory.mem0_client import get_mem0, is_mem0_enabled
        if not is_mem0_enabled():
            return []
        
        mem0 = get_mem0()
        if mem0 is None:
            return []
        
        filters: dict = {"user_id": user_id}
        if self._agent_name:
            filters["agent_id"] = self._agent_name
        
        # 可选：时间过滤
        mem_cfg = get_memory_config()
        if mem_cfg.mem0_enable_time_filter:
            cutoff = (datetime.now(UTC) - timedelta(days=mem_cfg.mem0_recent_days)).isoformat().replace("+00:00", "Z")
            filters["created_at"] = {"gte": cutoff}
        
        try:
            results = mem0.search(
                query=query,
                filters=filters,
                top_k=top_k,
            )
            # mem0 返回格式：{"results": [{"id":..., "memory":..., "score":...}]}
            if isinstance(results, dict):
                return results.get("results", [])
            elif isinstance(results, list):
                return results
            return []
        except Exception as e:
            logger.warning("mem0 search failed: %s", e)
            return []

    async def _search_mem0_async(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """异步包装 mem0.search()。"""
        return await asyncio.to_thread(self._search_mem0, query, user_id, top_k)

    async def _combined_search(self, first_message: str, user_id: str, top_k: int) -> list[dict]:
        """首回合组合查询：固定查询 + 首条消息，合并去重。"""
        mem_cfg = get_memory_config()
        
        # 并发两次 search
        general_task = self._search_mem0_async(mem_cfg.mem0_general_query, user_id, top_k)
        specific_task = self._search_mem0_async(first_message, user_id, top_k)
        general, specific = await asyncio.gather(general_task, specific_task)
        
        # 合并去重（按 memory id 或 content）
        seen_ids = set()
        merged = []
        for r in general + specific:
            mid = r.get("id") or r.get("memory", "")[:50]
            if mid not in seen_ids:
                seen_ids.add(mid)
                merged.append(r)
        
        # 按 score 排序，取 top_k
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:top_k]

    # ── 格式化记忆为注入文本 ──────────────────────────────────────────────

    def _format_memories(self, memories: list[dict]) -> str:
        """把 mem0 检索结果格式化为注入文本。"""
        if not memories:
            return ""
        
        lines = []
        for m in memories:
            content = m.get("memory", "")
            if content:
                lines.append(f"- {content}")
        return "\n".join(lines) if lines else ""

    # ── 构建 reminder ────────────────────────────────────────────────────

    def _build_reminder(self, memories_text: str, *, is_update: bool = False) -> str:
        """构建 system-reminder 文本。"""
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        
        memory_block = ""
        if memories_text:
            tag = "memory_update" if is_update else "memory"
            memory_block = f"<{tag}>\n{memories_text}\n</{tag}>\n\n"
        
        return (
            f"<system-reminder>\n"
            f"{memory_block}"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    def _build_date_only_reminder(self) -> str:
        """仅更新日期的轻量 reminder。"""
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return (
            f"<system-reminder>\n"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    # ── 消息操作工具 ──────────────────────────────────────────────────────

    @staticmethod
    def _make_reminder_and_user_messages(
        original: HumanMessage, reminder_content: str,
    ) -> tuple[HumanMessage, HumanMessage]:
        """ID-swap: reminder takes original ID, user gets derived ID。"""
        stable_id = original.id or str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=reminder_content,
            id=stable_id,
            additional_kwargs={
                "hide_from_ui": True,
                _DYNAMIC_CONTEXT_REMINDER_KEY: True,
            },
        )
        user_msg = HumanMessage(
            content=original.content,
            id=f"{stable_id}__user",
            name=original.name,
            additional_kwargs=original.additional_kwargs,
        )
        return reminder_msg, user_msg

    def _get_latest_user_message(self, messages: list) -> HumanMessage | None:
        """获取最新的非 reminder 用户消息。"""
        for msg in reversed(messages):
            if _is_user_injection_target(msg):
                return msg
        return None

    def _get_old_reminder_ids(self, messages: list) -> list[str]:
        """获取所有旧 reminder 的 message id（用于 RemoveMessage）。"""
        return [m.id for m in messages if is_dynamic_context_reminder(m)]

    # ── 主注入逻辑 ────────────────────────────────────────────────────────

    async def _inject(self, state: HarnessState) -> dict | None:
        """每轮都查都注入的核心逻辑。"""
        from harness.memory.mem0_client import is_mem0_enabled
        
        messages = list(state.get("messages", []))
        if not messages:
            return None
        
        user_id: str = state.get("user_id", "default")
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        
        # 找到最新的用户消息
        latest_user_msg = self._get_latest_user_message(messages)
        if latest_user_msg is None:
            return None
        
        mem_cfg = get_memory_config()
        
        # ── 分支 1：mem0 backend ── 每轮都查都注入 ──
        if is_mem0_enabled() and mem_cfg.injection_enabled:
            return await self._inject_mem0(state, messages, latest_user_msg, 
                                            user_id, current_date, mem_cfg)
        
        # ── 分支 2：file backend（向后兼容）── 保留原有逻辑 ──
        return self._inject_file_legacy(state, messages, user_id, current_date, mem_cfg)

    async def _inject_mem0(
        self, state: HarnessState, messages: list, 
        latest_user_msg: HumanMessage, user_id: str, current_date: str,
        mem_cfg,
    ) -> dict | None:
        """mem0 backend：每轮 search + RemoveMessage 替换。"""
        
        # 判断是否首回合（没有旧 reminder）
        old_reminder_ids = self._get_old_reminder_ids(messages)
        is_first_turn = len(old_reminder_ids) == 0
        
        # 获取最新用户消息内容
        user_content = latest_user_msg.content
        if not isinstance(user_content, str):
            user_content = str(user_content)
        
        # 检索记忆
        if is_first_turn:
            # 首回合：组合查询
            memories = await self._combined_search(
                user_content, user_id, mem_cfg.mem0_search_top_k
            )
            is_update = False
        else:
            # 后续回合：只用最新消息 search
            memories = await self._search_mem0_async(
                user_content, user_id, mem_cfg.mem0_search_top_k
            )
            is_update = True
        
        # 格式化记忆
        memories_text = self._format_memories(memories)
        
        # 构建 reminder
        reminder_content = self._build_reminder(memories_text, is_update=is_update)
        
        # 构建消息操作：先删旧 reminder，再追加新 reminder
        new_id = str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=reminder_content,
            id=new_id,
            additional_kwargs={
                "hide_from_ui": True,
                _DYNAMIC_CONTEXT_REMINDER_KEY: True,
            },
        )
        user_msg = HumanMessage(
            content=latest_user_msg.content,
            id=f"{new_id}__user",
            name=latest_user_msg.name,
            additional_kwargs=latest_user_msg.additional_kwargs,
        )
        
        # RemoveMessage 列表 + 新消息列表
        new_messages = [RemoveMessage(id=rid) for rid in old_reminder_ids]
        new_messages.append(reminder_msg)
        new_messages.append(user_msg)
        
        # 注意：需要同时移除旧 reminder 对应的 __user 派生消息
        # （因为 ID-swap 机制，user_msg 的 id 是 reminder_id + "__user"）
        for rid in old_reminder_ids:
            new_messages.insert(0, RemoveMessage(id=f"{rid}__user"))
        
        logger.info(
            "DynamicContextMiddleware[mem0]: %s turn — searched %d memories, "
            "replacing %d old reminders (user_id=%s)",
            "first" if is_first_turn else "subsequent",
            len(memories), len(old_reminder_ids), user_id,
        )
        
        return {
            "messages": new_messages,
            "memory_context": memories_text,
        }

    def _inject_file_legacy(
        self, state: HarnessState, messages: list, 
        user_id: str, current_date: str, mem_cfg,
    ) -> dict | None:
        """file backend：保留原有首回合注入+冻结逻辑（向后兼容）。"""
        last_date = _last_injected_date(messages)
        
        if last_date is None:
            # First turn: inject full reminder
            first_idx = next(
                (i for i, m in enumerate(messages) if _is_user_injection_target(m)),
                None,
            )
            if first_idx is None:
                return None
            
            # 用原有的 get_memory_data + format_memory_for_injection
            from harness.memory.updater import get_memory_data
            from harness.memory.prompt import format_memory_for_injection
            
            memory_context = ""
            memory_block = ""
            if mem_cfg.injection_enabled:
                try:
                    memory_data = get_memory_data(self._agent_name, user_id=user_id)
                    memory_context = format_memory_for_injection(
                        memory_data, max_tokens=mem_cfg.max_injection_tokens,
                    )
                    if memory_context:
                        memory_block = f"<memory>\n{memory_context}\n</memory>\n\n"
                except Exception as exc:
                    logger.warning("Failed to load memory for injection: %s", exc)
            
            reminder = (
                f"<system-reminder>\n"
                f"{memory_block}"
                f"<current_date>{current_date}</current_date>\n"
                f"</system-reminder>"
            )
            reminder_msg, user_msg = self._make_reminder_and_user_messages(
                messages[first_idx], reminder,
            )
            return {"messages": [reminder_msg, user_msg], "memory_context": memory_context}
        
        if last_date == current_date:
            return None
        
        # Midnight crossed
        last_human_idx = next(
            (i for i in reversed(range(len(messages)))
             if _is_user_injection_target(messages[i])),
            None,
        )
        if last_human_idx is None:
            return None
        
        reminder_msg, user_msg = self._make_reminder_and_user_messages(
            messages[last_human_idx], self._build_date_only_reminder(),
        )
        return {"messages": [reminder_msg, user_msg]}

    @override
    async def abefore_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        return await self._inject(state)
```

### 3.5 修改 MemoryMiddleware

**文件**：`harness/middleware/memory.py`

**改动要点**：基本不变，只是 queue 的处理会根据 backend 走不同路径。

在 `aafter_agent` 末尾增加 metadata（传时间信息）：

```python
# 在 queue.add() 调用前，增加时间 metadata
from datetime import UTC, datetime

current_time_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

queue.add(
    thread_id=thread_id,
    messages=filtered,
    agent_name=self._agent_name,
    user_id=user_id,
    correction_detected=correction_detected,
    reinforcement_detected=reinforcement_detected,
    # 新增：时间 metadata（mem0 backend 用）
    metadata={"event_time": current_time_iso, "thread_id": thread_id},
)
```

### 3.6 简化 MemoryUpdater

**文件**：`harness/memory/updater.py`

**改动要点**：新增 mem0 分支，调用 `mem0.add()`（内部含 LLM 提取+冲突检测）。

在 `aupdate_memory` 方法中增加 mem0 分支：

```python
async def aupdate_memory(self, messages, thread_id=None, agent_name=None,
                         correction_detected=False, reinforcement_detected=False,
                         user_id=None, metadata=None) -> bool:
    """Async entry point."""
    from harness.config.memory_config import get_memory_config
    from harness.memory.mem0_client import get_mem0, is_mem0_enabled
    
    cfg = get_memory_config()
    
    # ── mem0 backend ──
    if is_mem0_enabled():
        return await self._update_mem0(
            messages, user_id, agent_name, thread_id, 
            correction_detected, reinforcement_detected, metadata
        )
    
    # ── file backend（保留原有逻辑）──
    return await self._update_file(
        messages, thread_id, agent_name, 
        correction_detected, reinforcement_detected, user_id
    )

async def _update_mem0(self, messages, user_id, agent_name, thread_id,
                        correction_detected, reinforcement_detected, metadata) -> bool:
    """mem0 backend：直接调 mem0.add()，内部含 LLM 提取+冲突检测。"""
    import asyncio
    
    mem0 = get_mem0()
    if mem0 is None:
        logger.error("mem0 backend enabled but client not initialized")
        return False
    
    # 转换消息格式
    mem0_messages = []
    for m in messages:
        role = "user" if getattr(m, "type", None) == "human" else "assistant"
        content = m.content if isinstance(m.content, str) else str(m.content)
        if content.strip():
            mem0_messages.append({"role": role, "content": content})
    
    if not mem0_messages:
        return False
    
    # 构建 metadata
    mem_metadata = {"thread_id": thread_id or ""}
    if metadata:
        mem_metadata.update(metadata)
    
    # 纠正/强化信号作为 custom_instructions
    instructions = None
    if correction_detected:
        instructions = "Pay special attention to corrections in this conversation."
    elif reinforcement_detected:
        instructions = "Note the confirmed preferences in this conversation."
    
    try:
        await asyncio.to_thread(
            mem0.add,
            mem0_messages,
            user_id=user_id or "default",
            agent_id=agent_name,
            metadata=mem_metadata,
            # custom_instructions=instructions,  # Platform v3 专属，OSS 可能不支持
        )
        logger.info("mem0 add succeeded for user=%s agent=%s", user_id, agent_name)
        return True
    except Exception as e:
        logger.error("mem0 add failed: %s", e)
        return False

async def _update_file(self, messages, thread_id, agent_name,
                        correction_detected, reinforcement_detected, user_id) -> bool:
    """file backend：保留原有 LLM 提取逻辑。"""
    # ... 原有的 aupdate_memory 实现搬到这里 ...
```

### 3.7 修改 ConversationContext

**文件**：`harness/memory/queue.py`

在 `ConversationContext` dataclass 中新增 metadata 字段：

```python
@dataclass
class ConversationContext:
    """Context for a conversation to be processed for memory update."""
    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False
    metadata: dict | None = None  # 新增：mem0 用的时间等元数据
```

同步修改 `_enqueue_locked` 和 `_process_queue` 中对 `ConversationContext` 的构造，传入 metadata。

在 `_process_queue` 调用 `aupdate_memory` 时传 metadata：

```python
success = await updater.aupdate_memory(
    messages=context.messages,
    thread_id=context.thread_id,
    agent_name=context.agent_name,
    correction_detected=context.correction_detected,
    reinforcement_detected=context.reinforcement_detected,
    user_id=context.user_id,
    metadata=context.metadata,  # 新增
)
```

### 3.8 数据迁移脚本

**文件**：`scripts/migrate_memory_to_mem0.py`（新增）

```python
"""一次性迁移脚本：把现有 memory.json 的 facts 迁移到 mem0。

Usage:
    python scripts/migrate_memory_to_mem0.py [--memory-root ~/.multiagent-studio/memory]
"""

import asyncio
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.config.memory_config import set_memory_config, MemoryConfig
from harness.memory.mem0_client import get_mem0


async def migrate(memory_root: str = "~/.multiagent-studio/memory"):
    """迁移所有用户的 memory.json 到 mem0。"""
    root = Path(memory_root).expanduser()
    users_dir = root / "users"
    if not users_dir.exists():
        print(f"No users directory at {users_dir}")
        return
    
    mem0 = get_mem0()
    if mem0 is None:
        print("mem0 not initialized, check config")
        return
    
    total_users = 0
    total_facts = 0
    
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        mem_file = user_dir / "memory.json"
        if not mem_file.exists():
            continue
        
        data = json.loads(mem_file.read_text(encoding="utf-8"))
        
        # 迁移 facts
        facts = data.get("facts", [])
        for fact in facts:
            content = fact.get("content", "")
            if not content.strip():
                continue
            
            # 构建 metadata
            metadata = {
                "migrated_from": "file_storage",
                "category": fact.get("category", ""),
                "confidence": fact.get("confidence", 0.8),
            }
            # 如果有 createdAt，作为 event_time
            created = fact.get("createdAt") or fact.get("updatedAt")
            if created:
                metadata["event_time"] = created
            
            try:
                mem0.add(
                    f"用户事实：{content}",
                    user_id=user_id,
                    metadata=metadata,
                )
                total_facts += 1
            except Exception as e:
                print(f"  Failed to migrate fact for {user_id}: {e}")
        
        # 迁移 summaries（作为单条记忆）
        for section in ["user", "history"]:
            section_data = data.get(section, {})
            for key, val in section_data.items():
                summary = val.get("summary", "") if isinstance(val, dict) else ""
                if summary.strip():
                    try:
                        mem0.add(
                            f"{section}.{key}: {summary}",
                            user_id=user_id,
                            metadata={"migrated_from": "file_storage", "type": "summary"},
                        )
                        total_facts += 1
                    except Exception as e:
                        print(f"  Failed to migrate summary for {user_id}: {e}")
        
        total_users += 1
        print(f"Migrated user {user_id}: {len(facts)} facts")
    
    print(f"\nDone: {total_users} users, {total_facts} memories migrated")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-root", default="~/.multiagent-studio/memory")
    args = parser.parse_args()
    
    # 确保 mem0 backend 启用
    # （实际使用时需从 config.yaml 加载完整配置）
    asyncio.run(migrate(args.memory_root))
```

---

## 四、验证检查清单

修改完成后，按以下步骤验证：

### 4.1 基础验证
- [ ] `pip install mem0ai chromadb` 成功
- [ ] `from mem0 import Memory` 不报错
- [ ] config.yaml 设置 `backend: mem0` 后，`get_mem0()` 返回非 None
- [ ] config.yaml 设置 `backend: file` 后，走原有 file 逻辑

### 4.2 写入验证
- [ ] 发送一条消息，等 debounce 120s 后，Chroma 目录有数据
- [ ] 检查 mem0 的记忆是否正确提取了用户偏好
- [ ] 检查 metadata 中是否包含 event_time

### 4.3 检索验证
- [ ] 首回合：组合查询返回通用偏好 + 具体话题记忆
- [ ] 后续回合：用最新消息 search，返回相关记忆
- [ ] reminder 中包含 `<memory>` 或 `<memory_update>` 标签

### 4.4 RemoveMessage 验证
- [ ] 第二轮对话后，检查 messages 列表中只有 1 个 reminder（旧的被删除）
- [ ] LangGraph checkpoint 正确处理 RemoveMessage（state 恢复后 messages 干净）
- [ ] 不存在 reminder 累积现象

### 4.5 向后兼容验证
- [ ] config.yaml 设置 `backend: file` 后，原有 JSON 逻辑完全正常
- [ ] 现有的 summarization_hook（memory_flush_hook）仍能工作

---

## 五、注意事项

1. **RemoveMessage 与 add_messages reducer**：LangGraph 的 `add_messages` reducer 支持 `RemoveMessage`——当 messages 列表中包含 `RemoveMessage(id=xxx)` 时，reducer 会从 state 中删除对应 id 的消息。确保 LangGraph 版本支持此特性。

2. **ID-swap 机制的兼容性**：现有 `_make_reminder_and_user_messages` 做 ID 交换（reminder 占用原 ID，user 消息用 `原ID__user`）。RemoveMessage 时要同时删除这两个 ID（reminder 的 id 和 `reminder_id__user`）。

3. **mem0 的 search 是同步的**：mem0 OSS 的 `search()` 是同步阻塞调用，用 `asyncio.to_thread()` 包装避免阻塞事件循环。

4. **debounce 保留**：mem0 的 `add()` 每次 2 次 LLM 调用（提取+决策），保留现有 120s debounce 降低频率。

5. **Chroma 路径**：`path: ~/.multiagent-studio/chroma` 需要确保目录可写。Windows 上 `~` 需要展开为实际用户目录。

6. **embedding 模型维度**：Chroma 的 `embedding_model_dims` 必须与 embedder 模型匹配（text-embedding-3-small = 1536 维）。如果用其他 embedder，需确认维度。

7. **时间过滤是可选的**：`mem0_enable_time_filter: false` 时不过滤，检索所有时间的记忆。设为 true 时用 `created_at` 的 gte 过滤。注意 OSS 的 filters 支持有限，复杂过滤可能不生效。

8. **迁移脚本一次性运行**：迁移完成后，建议备份原 `memory.json`，不要立即删除（可回滚到 file backend）。

---

## 六、回滚方案

如果 mem0 接入出问题，回滚步骤：

1. `config.yaml` 改 `backend: file`
2. 重启服务
3. 原有 FileMemoryStorage 逻辑完全保留，可立即恢复

mem0 的 Chroma 数据可保留不删（不影响 file backend）。
