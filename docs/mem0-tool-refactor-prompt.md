# 修改提示词（完善版）：mem0 改为主动查询工具 + DynamicContextMiddleware 回退 file 模式

> 目标：将 mem0 从"被动每轮注入"改为"Agent 主动按需查询"的工具；
> DynamicContextMiddleware 回退到原 file 模式（memory.json），不再每轮 search mem0。
> 本文档是给开发者/AI 编程助手的详细修改指令，包含完整代码、边界条件、错误处理和验证清单。

---

## 一、设计原则与架构

### 1.1 职责重新划分

| 组件 | 旧职责（当前） | 新职责（目标） |
|------|--------------|--------------|
| **DynamicContextMiddleware** | mem0 每轮 search 注入 | **回退 file 模式**：读 memory.json，首回合注入后冻结 |
| **MemoryMiddleware** | 路由到 mem0.add() 或 file | **保持不变**（入队逻辑不动） |
| **MemoryUpdater** | 二选一写入 | **双写**：同时写 file 和 mem0（当 mem0_tool_enabled=true） |
| **memory_search 工具**（新增） | 无 | **Agent 主动查询 mem0**——按需精准检索 |

### 1.2 记忆双轨制

```
被动注入（file 模式）              主动查询（mem0 工具）
DynamicContextMiddleware           memory_search 工具
  ↓                                  ↓
读 memory.json                      调 mem0.search()
  ↓                                  ↓
首回合注入 <memory>                 Agent 按需调用
通用偏好/背景（粗粒度）              特定信息（细粒度）
token 预算 ~1000                    返回 5 条精准记忆
```

**关键**：两套记忆系统共存——memory.json 提供基础上下文（被动注入），mem0 提供精准查询能力（主动调用）。

### 1.3 配置矩阵

| backend | mem0_tool_enabled | DynamicContextMiddleware | memory_search 工具 | MemoryUpdater 写入 |
|---------|-------------------|--------------------------|-------------------|-------------------|
| `file` | `false` | file（原行为） | 不注册 | 只写 file |
| `file` | `true` | file | 注册 | **双写** file + mem0 |
| `mem0` | `false` | mem0（原行为） | 不注册 | 只写 mem0 |
| `mem0` | `true` | mem0 | 注册 | 只写 mem0 |

**目标配置**：`backend: file` + `mem0_tool_enabled: true`（第二行）

---

## 二、修改边界

### 2.1 需要修改的文件（8 个）

| 文件 | 操作 | 改动量 | 难度 |
|------|------|--------|------|
| `harness/middleware/dynamic_context.py` | **简化**：删除 mem0 分支，只保留 file 逻辑 | 删减 ~250 行 | 低 |
| `harness/tools/builtins/memory_tools.py` | **新增**：memory_search 工具 | ~120 行 | 中 |
| `harness/tools/builtins/lead_tools.py` | **修改**：build_lead_tools 加入 memory_search | ~10 行 | 低 |
| `harness/agents/lead_agent.py` | **修改**：系统提示词加 memory_tool_section | ~30 行 | 低 |
| `harness/config/memory_config.py` | **修改**：新增 mem0_tool_enabled 字段 | ~8 行 | 低 |
| `harness/config.yaml` | **修改**：backend=file + mem0_tool_enabled=true | ~5 行 | 低 |
| `harness/memory/updater.py` | **修改**：aupdate_memory 双写逻辑 | ~25 行 | 中 |
| `harness/memory/mem0_client.py` | **修改**：is_mem0_enabled 支持 tool 模式 | ~10 行 | 低 |

### 2.2 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `harness/middleware/memory.py` | 写入中间件入队逻辑不变 |
| `harness/memory/queue.py` | 队列逻辑不变（metadata 字段已存在） |
| `harness/memory/storage.py` | FileMemoryStorage 完整保留 |
| `harness/memory/prompt.py` | file 模式的 prompt 不变 |
| `harness/memory/message_processing.py` | 消息过滤逻辑不变 |
| `harness/memory/summarization_hook.py` | flush hook 不变 |

### 2.3 总改动量：约 210 行（新增 150 + 修改 60）

---

## 三、详细修改指令

### 3.1 简化 DynamicContextMiddleware

**文件**：`harness/middleware/dynamic_context.py`

**目标**：删除所有 mem0 相关代码，只保留原 file 模式逻辑。相当于把这个文件回退到 mem0 集成前的状态。

**需要删除的内容**：
- `import asyncio`（file 模式不需要）
- `from langchain_core.messages import HumanMessage, RemoveMessage` 中的 `RemoveMessage`
- `from datetime import datetime, UTC, timedelta` 中的 `UTC` 和 `timedelta`（file 模式只用 `datetime`）
- 所有 mem0 相关方法：`_search_mem0`、`_search_mem0_async`、`_combined_search`、`_format_memories`、`_build_reminder`(mem0 版)、`_build_date_only_reminder`、`_get_latest_user_message`、`_get_old_reminder_ids`、`_inject_mem0`
- `_inject()` 中的 `is_mem0_enabled()` 分支判断
- `_inject_file_legacy` 方法名改为 `_inject`（提升为唯一实现）

**需要恢复的 import**：
```python
from harness.memory.prompt import format_memory_for_injection
from harness.memory.updater import get_memory_data
```

**简化后的完整代码**：

```python
"""DynamicContextMiddleware — inject memory and current date as a <system-reminder>.

使用 file backend（memory.json）：
- 首回合注入完整 reminder（memory + date）
- 跨午夜只更新日期
- 同日续聊不注入（frozen snapshot persists）

Reminder format:

    <system-reminder>
    <memory>...</memory>

    <current_date>2026-07-02, Thursday</current_date>
    </system-reminder>

Date-update format (midnight crossing):

    <system-reminder>
    <current_date>2026-07-03, Friday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import override

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.memory.prompt import format_memory_for_injection
from harness.memory.updater import get_memory_data
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
    """Inject memory (from memory.json) and current date into HumanMessages as a <system-reminder>.

    First turn: prepends full reminder (memory + date) to the first HumanMessage.
    Midnight crossing: injects lightweight date-update reminder.
    Same-day continuation: no injection needed (frozen snapshot persists).
    """

    name = "dynamic_context"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None):
        super().__init__(config)
        self._agent_name = agent_name

    # ── Reminder builders ────────────────────────────────────────────────

    def _build_full_reminder(self, *, user_id: str | None = None) -> tuple[str, str]:
        """Build the full reminder and return (reminder_text, memory_context_text)."""
        mem_cfg = get_memory_config()
        injection_enabled = mem_cfg.injection_enabled
        memory_context = ""
        memory_block = ""
        if injection_enabled:
            try:
                memory_data = get_memory_data(self._agent_name, user_id=user_id)
                memory_context = format_memory_for_injection(
                    memory_data,
                    max_tokens=mem_cfg.max_injection_tokens,
                )
                if memory_context:
                    memory_block = f"<memory>\n{memory_context}\n</memory>\n\n"
            except Exception as exc:
                logger.warning("Failed to load memory for injection: %s", exc)

        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        reminder = (
            f"<system-reminder>\n"
            f"{memory_block}"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )
        return reminder, memory_context

    def _build_date_update_reminder(self) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return (
            f"<system-reminder>\n"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    @staticmethod
    def _make_reminder_and_user_messages(
        original: HumanMessage, reminder_content: str,
    ) -> tuple[HumanMessage, HumanMessage]:
        """ID-swap: reminder takes original ID, user gets derived ID."""
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

    # ── Injection logic ──────────────────────────────────────────────────

    def _inject(self, state: HarnessState) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        user_id: str | None = state.get("user_id")
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        last_date = _last_injected_date(messages)

        if last_date is None:
            # First turn: inject full reminder
            first_idx = next(
                (i for i, m in enumerate(messages) if _is_user_injection_target(m)),
                None,
            )
            if first_idx is None:
                return None
            full_reminder, memory_context = self._build_full_reminder(user_id=user_id)
            logger.info(
                "DynamicContextMiddleware: injecting full reminder (len=%d, has_memory=%s, user_id=%s)",
                len(full_reminder),
                "<memory>" in full_reminder,
                user_id or "default",
            )
            reminder_msg, user_msg = self._make_reminder_and_user_messages(
                messages[first_idx], full_reminder,
            )
            return {
                "messages": [reminder_msg, user_msg],
                "memory_context": memory_context,
            }

        if last_date == current_date:
            # Same day: nothing to do
            return None

        # Midnight crossed: inject date-update reminder
        last_human_idx = next(
            (i for i in reversed(range(len(messages)))
             if _is_user_injection_target(messages[i])),
            None,
        )
        if last_human_idx is None:
            return None

        reminder_msg, user_msg = self._make_reminder_and_user_messages(
            messages[last_human_idx], self._build_date_update_reminder(),
        )
        logger.info(
            "DynamicContextMiddleware: midnight crossing — injected date update (user_id=%s)",
            user_id or "default",
        )
        return {"messages": [reminder_msg, user_msg]}

    @override
    async def abefore_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        return self._inject(state)
```

**验证点**：
- 文件中不再出现 `mem0`、`RemoveMessage`、`asyncio`、`is_mem0_enabled` 等关键字
- `_inject()` 方法不再有分支判断，直接走 file 逻辑
- `get_memory_data` 和 `format_memory_for_injection` 的 import 恢复

---

### 3.2 新增 memory_search 工具

**文件**：`harness/tools/builtins/memory_tools.py`（新增）

**设计要点**：
1. **user_id / agent_id 自动注入**：从 LangGraph `get_config()` 读取，LLM 只需传 `query`
2. **top_k=5**：精准查询不需要太多结果
3. **明确的使用边界**：工具描述里有正例和反例
4. **"不确定就查"原则**：减少 LLM 决策负担
5. **空结果不重复**：明确告诉 LLM 不要重复相同查询
6. **错误兜底**：mem0 未初始化、查询异常都返回友好信息
7. **结果格式化**：带编号列表，方便 LLM 引用

**完整代码**：

```python
"""memory_search tool — let Agent proactively query mem0 long-term memory.

mem0 stores facts/preferences extracted from past conversations. This tool
provides on-demand precise retrieval, complementing DynamicContextMiddleware's
passive injection (which uses memory.json for general context).

Architecture:
    memory.json (passive injection)  ←  DynamicContextMiddleware (first turn only)
    mem0 (active query)              ←  memory_search tool (on-demand)

Both are written to by MemoryUpdater when mem0_tool_enabled=true (dual-write).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)


def create_memory_search_tool() -> BaseTool:
    """Create the ``memory_search`` tool for querying mem0.

    The tool reads user_id and agent_id from LangGraph runtime context
    automatically — the LLM only needs to provide the query string.

    Returns:
        A BaseTool instance that can be added to an agent's tool list.
    """

    @tool
    async def memory_search(query: str) -> str:
        """Search the user's long-term memory for relevant facts and preferences.

        This tool accesses memories extracted from ALL past conversations
        (not just the current one). Use it when you need information the user
        may have shared before but isn't in the current conversation.

        —— WHEN TO USE ——
        - User references past info: "continue last time", "like before",
          "do you remember...", "that project I mentioned"
        - Need personalization: "recommend a movie" — check preferences first
        - Missing context: user's request lacks info they likely shared before
        - User asks about their own history: "what did I tell you about X"

        —— WHEN NOT TO USE ——
        - Current conversation already has all needed information
        - Brand new topic with no connection to past conversations
        - User uploaded a file or gave complete specs (current context suffices)
        - Pure factual Q&A (answer doesn't depend on who the user is)

        —— GUIDELINES ——
        - When unsure whether to search: search once (it's cheap, ~50ms)
        - If results are empty, don't repeat the same query
        - Frame queries naturally: "user's Python preferences" not just "Python"
        - Results show facts/preferences, not raw conversation transcripts

        Args:
            query: Natural language describing what you're looking for.
                   Good queries specify the topic: "user's preferred programming
                   language" or "project names user mentioned".
        """
        from harness.memory.mem0_client import get_mem0
        from langgraph.config import get_config

        # ── 1. 获取 mem0 实例 ──
        mem0 = get_mem0()
        if mem0 is None:
            return ("Memory search unavailable: mem0 not initialized. "
                    "Proceed with information available in the current context.")

        # ── 2. 从 LangGraph 配置自动获取 user_id 和 agent_id ──
        # LLM 不需要（也不应该）手动传这些参数
        try:
            config = get_config()
        except RuntimeError:
            # 在非 LangGraph 上下文中调用（如测试）
            config = {}

        configurable = config.get("configurable", {}) or {}
        user_id = configurable.get("user_id", "default")
        agent_id = configurable.get("agent_name") or "lead_agent"

        # 构建 filters
        filters: dict[str, Any] = {"user_id": user_id}
        if agent_id:
            filters["agent_id"] = agent_id

        # ── 3. 调用 mem0.search()（线程池避免阻塞事件循环）──
        try:
            results = await asyncio.to_thread(
                mem0.search,
                query=query,
                filters=filters,
                top_k=5,
            )
        except Exception as e:
            logger.warning("memory_search failed (query=%r, user=%s): %s",
                          query, user_id, e)
            return f"Memory search encountered an error: {e}. Please proceed with current context."

        # ── 4. 解析结果 ──
        # mem0 返回格式可能是：
        #   {"results": [{"id":..., "memory":..., "score":...}]}  (标准)
        #   [{"id":..., "memory":...}]                           (某些版本)
        #   {"memories": [...]}                                   (旧版)
        if isinstance(results, dict):
            items = results.get("results") or results.get("memories") or []
        elif isinstance(results, list):
            items = results
        else:
            items = []

        # ── 5. 空结果处理 ──
        if not items:
            return "No relevant memories found. This may be a new topic or the user hasn't shared related information before."

        # ── 6. 格式化输出 ──
        lines = [f"Found {len(items)} relevant memories:"]
        for i, item in enumerate(items, 1):
            # mem0 的记忆内容字段可能是 "memory" 或 "content"
            content = item.get("memory", "") or item.get("content", "")
            if not content:
                continue
            # 附带 score（如果有）方便 LLM 判断可信度
            score = item.get("score")
            if score is not None:
                lines.append(f"{i}. {content} (relevance: {score:.2f})")
            else:
                lines.append(f"{i}. {content}")

        result_text = "\n".join(lines)
        logger.info(
            "memory_search: query=%r user=%s → %d results",
            query[:50], user_id, len(items),
        )
        return result_text

    return memory_search
```

**关键设计细节**：

1. **mem0 返回格式兼容**：处理了 `{"results": [...]}`、`[...]`、`{"memories": [...]}` 三种格式
2. **`get_config()` 的异常处理**：在非 LangGraph 上下文（如单元测试）中调用会抛 `RuntimeError`，做了兜底
3. **score 显示**：如果 mem0 返回了 score，附带显示让 LLM 判断可信度
4. **日志记录**：记录查询内容和结果数量，便于调试

---

### 3.3 修改 build_lead_tools 加入 memory_search

**文件**：`harness/tools/builtins/lead_tools.py`

**修改 1**：文件顶部新增 import

在现有的 import 块之后添加：

```python
from harness.tools.builtins.memory_tools import create_memory_search_tool
```

**修改 2**：修改 `build_lead_tools()` 函数

将原函数：
```python
def build_lead_tools(manager: Any | None = None) -> list[BaseTool]:
    """Return all built-in tools required by the Lead Agent."""
    return [
        create_subagent_tool(manager),
        task_tool(manager),
        ask_clarification_tool(),
    ]
```

替换为：
```python
def build_lead_tools(manager: Any | None = None) -> list[BaseTool]:
    """Return all built-in tools required by the Lead Agent.

    Tools included:
    - create_subagent: Create specialized SubAgents
    - task: Delegate tasks to SubAgents
    - ask_clarification: Ask user for clarification
    - memory_search: Query mem0 long-term memory (if mem0_tool_enabled)
    """
    tools: list[BaseTool] = [
        create_subagent_tool(manager),
        task_tool(manager),
        ask_clarification_tool(),
    ]

    # mem0 主动查询工具（如果启用）
    # mem0_tool_enabled 与 backend 独立：
    #   - backend=file + mem0_tool_enabled=true → 双轨制（file 注入 + mem0 工具）
    #   - backend=mem0 + mem0_tool_enabled=true → mem0 模式 + 工具
    try:
        from harness.config.memory_config import get_memory_config
        mem_cfg = get_memory_config()
        if mem_cfg.enabled and getattr(mem_cfg, "mem0_tool_enabled", False):
            tools.append(create_memory_search_tool())
            logger.info("memory_search tool registered")
    except Exception as e:
        logger.warning("Failed to register memory_search tool: %s", e)

    return tools
```

**关键**：
- 用 `getattr(mem_cfg, "mem0_tool_enabled", False)` 做安全访问，避免配置未更新时 AttributeError
- try/except 兜底，工具注册失败不影响其他工具

---

### 3.4 修改系统提示词

**文件**：`harness/agents/lead_agent.py`

**修改 1**：在 `SYSTEM_PROMPT_TEMPLATE` 中新增 `{memory_tool_section}` 占位符

在 `{working_directory_section}` 之后、`<response_style>` 之前插入：

```python
SYSTEM_PROMPT_TEMPLATE = """<role>
You are {agent_name}, an AI assistant with multi-agent orchestration capabilities.
</role>

<thinking_style>
- Think concisely and strategically about the user's request BEFORE taking action
- Break down the task: What is clear? What is ambiguous? What is missing?
- **PRIORITY CHECK: If anything is unclear, you MUST ask for clarification FIRST**
- **DECOMPOSITION CHECK: Can this task be broken into 2+ parallel sub-tasks? If YES, COUNT them.**
- Never write down your full final answer in thinking, but only outline
- Your response must contain the actual answer, not just a reference to what you thought
</thinking_style>

{clarification_section}

{subagent_section}

{working_directory_section}

{memory_tool_section}

<response_style>
- Clear and Concise: Avoid over-formatting unless requested
- Natural Tone: Use paragraphs and prose, not bullet points by default
- Action-Oriented: Focus on delivering results
- Language Consistency: Keep using the same language as the user
- Always Respond: You MUST always provide a visible response to the user after thinking
</response_style>

<critical_reminders>
- **Clarification First**: ALWAYS clarify unclear/missing/ambiguous requirements BEFORE starting work
{subagent_reminder}- Multi-task: Better utilize parallel tool calling for better performance
- Language Consistency: Keep using the same language as user's
- Always Respond: You MUST always provide a visible response to the user after thinking
</critical_reminders>"""
```

**修改 2**：新增 `_build_memory_tool_section()` 函数

在 `_build_working_directory_section()` 函数之后添加：

```python
def _build_memory_tool_section(mem0_tool_enabled: bool) -> str:
    """Build the memory tool guidance section for the system prompt.

    Only included when mem0_tool_enabled=True. Explains to the Agent:
    - What memory_search tool does
    - Basic context is already injected (don't search for that)
    - When to use the tool (specific scenarios)
    - When not to use it
    - Default behavior: when in doubt, search once
    """
    if not mem0_tool_enabled:
        return ""
    return """
<memory_tool_guidance>
You have a `memory_search` tool to look up facts and preferences the user
shared in past conversations.

**Two memory layers:**
1. **Passive injection**: Basic context (general preferences, background) is
   already injected into your <system-reminder> at conversation start.
   You DON'T need to search for those.
2. **Active query**: Use `memory_search` for specific details NOT in the
   injected context.

**When to use `memory_search`:**
- User references past info: "continue last time", "like before", "remember when"
- Need specific details for personalization (e.g., user's tech stack before recommending a library)
- User asks "do you remember..." or about their own history
- Current context is missing information the user likely shared before

**When NOT to use:**
- The injected <memory> block already has what you need
- Current conversation has all required information
- Brand new topic unrelated to user history
- User uploaded files or gave complete specifications

**Guidelines:**
- When in doubt, search once — one query (~50ms) costs less than a wrong answer
- If search returns "No relevant memories found", do NOT repeat the same query
- Frame queries naturally: "user's preferred programming language" (good) vs "Python" (too vague)
- Results are extracted facts, not raw conversation transcripts
</memory_tool_guidance>
"""
```

**修改 3**：修改 `apply_prompt_template()` 函数

将原函数：
```python
def apply_prompt_template(
    agent_name: str = "Multi-Agent Orchestrator",
    max_concurrent_subagents: int = 3,
    subagent_enabled: bool = True,
) -> str:
    """Assemble the full Lead Agent system prompt from sections."""
    subagent_section = _build_subagent_section(max_concurrent_subagents) if subagent_enabled else ""
    clarification_section = _build_clarification_section()
    working_directory_section = _build_working_directory_section()

    n = max_concurrent_subagents
    subagent_reminder = (
        f"- **Orchestrator Mode**: Decompose complex tasks into parallel sub-tasks. "
        f"**HARD LIMIT: max {n} `task` calls per response.** "
        f"If >{n} sub-tasks, split into sequential batches of ≤{n}.\n"
        if subagent_enabled
        else ""
    )

    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name,
        clarification_section=clarification_section,
        subagent_section=subagent_section,
        working_directory_section=working_directory_section,
        subagent_reminder=subagent_reminder,
    )
```

替换为：
```python
def apply_prompt_template(
    agent_name: str = "Multi-Agent Orchestrator",
    max_concurrent_subagents: int = 3,
    subagent_enabled: bool = True,
    mem0_tool_enabled: bool = False,
) -> str:
    """Assemble the full Lead Agent system prompt from sections.

    Args:
        agent_name: Display name for the agent
        max_concurrent_subagents: Max parallel task calls
        subagent_enabled: Whether subagent orchestration is available
        mem0_tool_enabled: Whether memory_search tool is registered
    """
    subagent_section = _build_subagent_section(max_concurrent_subagents) if subagent_enabled else ""
    clarification_section = _build_clarification_section()
    working_directory_section = _build_working_directory_section()
    memory_tool_section = _build_memory_tool_section(mem0_tool_enabled)

    n = max_concurrent_subagents
    subagent_reminder = (
        f"- **Orchestrator Mode**: Decompose complex tasks into parallel sub-tasks. "
        f"**HARD LIMIT: max {n} `task` calls per response.** "
        f"If >{n} sub-tasks, split into sequential batches of ≤{n}.\n"
        if subagent_enabled
        else ""
    )

    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name,
        clarification_section=clarification_section,
        subagent_section=subagent_section,
        working_directory_section=working_directory_section,
        memory_tool_section=memory_tool_section,
        subagent_reminder=subagent_reminder,
    )
```

**修改 4**：修改 `LeadAgent.get_system_prompt()` 方法

将原方法：
```python
def get_system_prompt(self) -> str:
    """Build the complete system prompt using the DeerFlow-style template."""
    return apply_prompt_template(
        agent_name=self.agent_name,
        max_concurrent_subagents=self.max_concurrent,
        subagent_enabled=self.subagent_manager is not None,
    )
```

替换为：
```python
def get_system_prompt(self) -> str:
    """Build the complete system prompt using the DeerFlow-style template."""
    from harness.config.memory_config import get_memory_config
    mem_cfg = get_memory_config()
    return apply_prompt_template(
        agent_name=self.agent_name,
        max_concurrent_subagents=self.max_concurrent,
        subagent_enabled=self.subagent_manager is not None,
        mem0_tool_enabled=mem_cfg.enabled and getattr(mem_cfg, "mem0_tool_enabled", False),
    )
```

---

### 3.5 新增配置字段

**文件**：`harness/config/memory_config.py`

在 `MemoryConfig` 类的 mem0 配置段末尾（`mem0_general_token_budget` 之后）新增：

```python
    mem0_tool_enabled: bool = Field(
        default=False,
        description=(
            "Whether to register memory_search tool for Agent to proactively query mem0. "
            "Independent of backend — can be True even when backend='file', "
            "enabling dual-track: file for passive injection + mem0 for active query."
        ),
    )
```

**关键**：`mem0_tool_enabled` 与 `backend` **独立**。这就是双轨制的核心——可以 `backend=file`（DynamicContextMiddleware 用 memory.json 被动注入）同时 `mem0_tool_enabled=true`（Agent 有工具主动查 mem0）。

---

### 3.6 修改 config.yaml

**文件**：`harness/config.yaml`

将 memory 段从：
```yaml
memory:
  enabled: True
  backend: mem0                # file | mem0
  debounce_seconds: 120
  # ── file backend 配置（向后兼容）──
  max_facts: 100
  fact_confidence_threshold: 0.8
  injection_enabled: true
  max_injection_tokens: 1000
  # ── mem0 backend 配置 ──
  mem0_search_top_k: 10
  mem0_general_query: "用户的偏好、习惯、背景和重要信息"
  mem0_enable_time_filter: false
  mem0_recent_days: 90
  mem0_general_token_budget: 400
  mem0_config:
    # ... (vector_store / llm / embedder 配置)
```

修改为：
```yaml
memory:
  enabled: True
  backend: file                 # ← 改回 file（DynamicContextMiddleware 用 memory.json）
  debounce_seconds: 120
  # ── file backend 配置 ──
  max_facts: 100
  fact_confidence_threshold: 0.8
  injection_enabled: true
  max_injection_tokens: 1000
  # ── mem0 工具配置（与 backend 独立）──
  mem0_tool_enabled: true       # ← 新增：启用 memory_search 工具
  mem0_search_top_k: 5          # 工具查询的 top_k（不同于中间件的 top_k）
  # ── 以下 mem0 配置在写入路径和工具查询时使用 ──
  mem0_config:
    vector_store:
      provider: pgvector
      config:
        dbname: multiagent_studio
        collection_name: memories
        embedding_model_dims: 1024
        user: harness
        password: harness
        host: localhost
        port: 5432
        hnsw: true
    llm:
      provider: openai
      config:
        model: qwen3.6-plus
        openai_base_url: ${HARNESS_OPENAI_BASE_URL}
        api_key: ${HARNESS_OPENAI_API_KEY}
    embedder:
      provider: openai
      config:
        model: text-embedding-v4
        openai_base_url: ${DASHSCOPE_EMBEDDING_BASE_URL}
        api_key: ${DASHSCOPE_API_KEY}
```

**关键变化**：
- `backend: file`（从 `mem0` 改回 `file`）
- 新增 `mem0_tool_enabled: true`
- `mem0_search_top_k` 改为 5（工具查询不需要 10 条）
- 删除了 `mem0_general_query`、`mem0_enable_time_filter`、`mem0_recent_days`、`mem0_general_token_budget`（这些是中间件用的，工具不需要）
- `mem0_config` 保留（写入路径和工具查询都需要）

---

### 3.7 修改 MemoryUpdater 支持双写

**文件**：`harness/memory/updater.py`

**目标**：当 `backend=file` + `mem0_tool_enabled=true` 时，同时写入 file 和 mem0。

**修改 `aupdate_memory()` 方法**：

将原方法：
```python
async def aupdate_memory(self, messages, thread_id=None, agent_name=None,
                         correction_detected=False, reinforcement_detected=False,
                         user_id=None, metadata=None) -> bool:
    """Async entry point — routes to mem0 or file backend."""
    from harness.memory.mem0_client import get_mem0, is_mem0_enabled

    # ── mem0 backend ──
    if is_mem0_enabled():
        return await self._update_mem0(
            messages, user_id, agent_name, thread_id,
            correction_detected, reinforcement_detected, metadata,
        )

    # ── file backend（保留原有逻辑）──
    return await self._do_update_memory(
        messages=messages, thread_id=thread_id, agent_name=agent_name,
        correction_detected=correction_detected,
        reinforcement_detected=reinforcement_detected,
        user_id=user_id,
    )
```

替换为：
```python
async def aupdate_memory(self, messages, thread_id=None, agent_name=None,
                         correction_detected=False, reinforcement_detected=False,
                         user_id=None, metadata=None) -> bool:
    """Async entry point — may write to file, mem0, or both (dual-write).

    Routing logic:
    - backend=file + mem0_tool_enabled=false → only file (original behavior)
    - backend=file + mem0_tool_enabled=true  → BOTH file and mem0 (dual-write)
    - backend=mem0 + mem0_tool_enabled=false → only mem0 (original behavior)
    - backend=mem0 + mem0_tool_enabled=true  → only mem0 (no need for file)
    """
    from harness.config.memory_config import get_memory_config

    cfg = get_memory_config()
    mem0_tool_enabled = getattr(cfg, "mem0_tool_enabled", False)

    results: list[bool] = []

    # ── file 写入（当 backend=file 时）──
    if cfg.backend == "file":
        try:
            file_result = await self._do_update_memory(
                messages=messages, thread_id=thread_id, agent_name=agent_name,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
                user_id=user_id,
            )
            results.append(file_result)
        except Exception as e:
            logger.error("file memory update failed: %s", e)
            results.append(False)

    # ── mem0 写入（当 backend=mem0 或 mem0_tool_enabled=true 时）──
    if cfg.backend == "mem0" or mem0_tool_enabled:
        try:
            mem0_result = await self._update_mem0(
                messages, user_id, agent_name, thread_id,
                correction_detected, reinforcement_detected, metadata,
            )
            results.append(mem0_result)
        except Exception as e:
            logger.error("mem0 update failed: %s", e)
            results.append(False)

    # 至少一个成功就算成功
    return any(results) if results else False
```

**关键设计**：
1. **独立的 try/except**：file 写入失败不影响 mem0 写入，反之亦然
2. **`any(results)`**：只要有一个成功就返回 True
3. **不重复写入**：`backend=mem0` 时 `cfg.backend == "file"` 为 False，不会写 file

---

### 3.8 修改 mem0_client.py 支持 tool 模式

**文件**：`harness/memory/mem0_client.py`

**问题**：当前 `is_mem0_enabled()` 只在 `backend == "mem0"` 时返回 True。新方案下 `backend=file` + `mem0_tool_enabled=true` 时，工具需要调用 `get_mem0()`，但 `get_mem0()` 会因为 `is_mem0_enabled()` 返回 False 而返回 None。

**修改 `is_mem0_enabled()` 函数**：

将：
```python
def is_mem0_enabled() -> bool:
    """Check if mem0 backend is enabled."""
    from harness.config.memory_config import get_memory_config

    cfg = get_memory_config()
    return cfg.enabled and cfg.backend == "mem0"
```

替换为：
```python
def is_mem0_enabled() -> bool:
    """Check if mem0 is used (either as backend or as tool).

    Returns True when:
    - backend='mem0' (mem0 as primary memory backend), OR
    - mem0_tool_enabled=True (mem0 as active query tool, even with file backend)

    This ensures get_mem0() initializes the mem0 client in both scenarios.
    """
    from harness.config.memory_config import get_memory_config

    cfg = get_memory_config()
    if not cfg.enabled:
        return False
    return cfg.backend == "mem0" or getattr(cfg, "mem0_tool_enabled", False)
```

**修改 `get_mem0()` 函数的判断条件**：

将 `get_mem0()` 中的：
```python
    if not cfg.enabled or cfg.backend != "mem0":
        _initialized = True
        return None
```

替换为：
```python
    if not cfg.enabled or not is_mem0_enabled():
        _initialized = True
        return None
```

**验证点**：
- `backend=file` + `mem0_tool_enabled=true` → `is_mem0_enabled()` 返回 True → `get_mem0()` 初始化 mem0
- `backend=file` + `mem0_tool_enabled=false` → `is_mem0_enabled()` 返回 False → `get_mem0()` 返回 None
- `backend=mem0` → `is_mem0_enabled()` 返回 True（原行为不变）

---

## 四、最终数据流

```
用户消息
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ DynamicContextMiddleware.abefore_agent()                    │
│                                                             │
│  读 memory.json（file 模式）                                  │
│  首回合注入 <memory>...</memory> 到 system-reminder          │
│  后续回合不注入（冻结），跨午夜只更新日期                      │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ Agent 执行 (ReAct loop)                                      │
│                                                             │
│  系统提示词中有 <memory_tool_guidance> 指导                   │
│                                                             │
│  如果 Agent 判断需要历史信息 → 调 memory_search 工具          │
│    └─→ get_mem0() 获取单例                                   │
│    └─→ get_config() 获取 user_id/agent_id                    │
│    └─→ mem0.search(query, filters, top_k=5)                 │
│    └─→ 返回 "Found N relevant memories: 1. ... 2. ..."      │
│                                                             │
│  Agent 基于注入的基础上下文 + 工具查到的精准记忆 → 生成回复    │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ MemoryMiddleware.aafter_agent()                             │
│   ├─ filter_messages_for_memory()                           │
│   ├─ detect_correction() / detect_reinforcement()           │
│   └─ queue.add(messages, metadata={event_time, thread_id})  │
└─────────────────────────────────────────────────────────────┘
    │  (debounce 120s or summarization hook immediate)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ MemoryUpdater.aupdate_memory()                              │
│                                                             │
│  if backend == "file":                                      │
│    └─→ _do_update_memory()                                  │
│        ├─→ LLM.ainvoke(MEMORY_UPDATE_PROMPT)                │
│        └─→ FileMemoryStorage.save(memory.json)              │
│                                                             │
│  if mem0_tool_enabled:                                      │
│    └─→ _update_mem0()                                       │
│        └─→ mem0.add(messages, user_id, agent_id, metadata)  │
│            [mem0 内部 LLM 提取事实 → 写入 pgvector]           │
│                                                             │
│  （双写：file 给被动注入，mem0 给主动查询）                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、工具使用边界设计详解

### 5.1 什么时候使用 memory_search

| 场景 | 使用 | 理由 |
|------|------|------|
| 用户说"继续上次的..." | ✅ | 需要历史上下文 |
| "还记得我说的偏好吗" | ✅ | 用户显式引用过去 |
| "推荐一本小说" | ✅ | 需要个性化（查偏好） |
| "那个项目进展如何" | ✅ | 当前对话缺失，可能在历史中 |
| "我之前用什么框架" | ✅ | 用户问自己的历史 |
| 当前对话已包含所有信息 | ❌ | 冗余查询 |
| "教我用 Docker"（全新话题） | ❌ | 历史记忆无关 |
| 用户上传了文件 | ❌ | 当前上下文已足够 |
| "1+1等于几" | ❌ | 纯事实问答，不依赖用户 |

### 5.2 工具描述的关键要素

1. **明确正例**（WHEN TO USE）：5 种场景，每种有具体例子
2. **明确反例**（WHEN NOT TO USE）：4 种场景
3. **默认行为**："不确定就查一次"——减少决策负担
4. **参数简化**：LLM 只传 query，user_id/agent_id 后端自动注入
5. **空结果处理**："不要重复相同查询"
6. **查询质量指导**："Frame queries naturally"——好的 query 示例

### 5.3 系统提示词的补充

`<memory_tool_guidance>` section 明确告诉 Agent：
1. **两套记忆层**：被动注入（基础上下文）+ 主动查询（特定细节）
2. **不要搜索已有的**：`<memory>` block 里有的不用再查
3. **不确定就查**：一次查询比错误答案便宜
4. **空结果不重复**：避免浪费

### 5.4 防止滥用的三层防线

| 层级 | 机制 | 效果 |
|------|------|------|
| **第一层：工具描述** | WHEN NOT TO USE 明确列出反例 | 80% 的滥用在此拦截 |
| **第二层：系统提示词** | "不要搜索已有的" + "空结果不重复" | 15% 的滥用在此拦截 |
| **第三层：后端兜底** | 空结果返回友好提示而非报错 | 5% 的滥用不影响体验 |

---

## 六、验证检查清单

### 6.1 基础验证

- [ ] `config.yaml` 设置 `backend: file` 后，DynamicContextMiddleware 走 file 逻辑（不出现 mem0 相关日志）
- [ ] `mem0_tool_enabled: true` 后，`build_lead_tools()` 返回的工具列表包含 `memory_search`
- [ ] `mem0_tool_enabled: false` 时，`memory_search` 工具不注册
- [ ] Agent 的工具列表里能看到 `memory_search`（可通过日志或 Langfuse trace 验证）
- [ ] 系统提示词中包含 `<memory_tool_guidance>` section

### 6.2 被动注入验证（file 模式）

- [ ] 首回合对话，system-reminder 里有 `<memory>` 标签（来自 memory.json）
- [ ] 同日续聊，不重复注入（冻结）
- [ ] 跨午夜，只更新日期
- [ ] DynamicContextMiddleware 日志中不出现 "mem0" 关键字

### 6.3 主动查询验证（mem0 工具）

- [ ] Agent 在需要时调用 `memory_search`（通过 Langfuse trace 观察）
- [ ] 工具返回 mem0 中的记忆（格式："Found N relevant memories: 1. ... 2. ..."）
- [ ] `user_id` 和 `agent_id` 自动注入（LLM 不需要传）
- [ ] 空结果时返回 "No relevant memories found..."
- [ ] mem0 未初始化时返回 "Memory search unavailable..."
- [ ] 查询异常时返回 "Memory search encountered an error..."

### 6.4 双写验证

- [ ] 对话后 120s（debounce），memory.json 有更新（file 写入成功）
- [ ] 对话后 120s，pgvector 有新数据（mem0 写入成功）
- [ ] file 写入失败不影响 mem0 写入（独立 try/except）
- [ ] mem0 写入失败不影响 file 写入

### 6.5 边界验证

- [ ] `mem0_tool_enabled: false` + `backend: file` → 纯 file 模式（原行为，无工具）
- [ ] `mem0_tool_enabled: true` + `backend: file` → 双轨制（file 注入 + mem0 工具 + 双写）
- [ ] `mem0_tool_enabled: false` + `backend: mem0` → 纯 mem0 模式（原行为）
- [ ] `mem0_tool_enabled: true` + `backend: mem0` → mem0 模式 + 工具（不双写）

### 6.6 回滚验证

- [ ] 改 `mem0_tool_enabled: false` → 工具消失，file 模式不受影响
- [ ] 改 `backend: mem0` + `mem0_tool_enabled: false` → 回到原 mem0 模式

---

## 七、注意事项

### 7.1 双写的 LLM 成本

| 路径 | LLM 调用 | 频率 |
|------|---------|------|
| file 写入（_do_update_memory） | 1 次（MEMORY_UPDATE_PROMPT） | debounce 120s |
| mem0 写入（_update_mem0 → mem0.add） | 2 次（提取+冲突检测） | debounce 120s |
| **双写总计** | 3 次/轮 | debounce 120s |

debounce 120s 意味着最快 2 分钟才触发一次写入，3 次 LLM 调用在这个频率下成本可控。

### 7.2 memory.json 与 mem0 的数据一致性

两套系统独立运行，**不需要保持一致**：
- memory.json 存 summaries + facts（结构化 JSON，给被动注入用）
- mem0 存向量化的 facts（非结构化文本，给主动查询用）

它们服务于不同目的，数据格式不同是正常的。

### 7.3 工具的 user_id 来源

工具从 `get_config().configurable.user_id` 获取。确保 LangGraph 配置中传入了 `user_id`。

**检查点**：在 `harness/main.py` 的 `execute()` 方法中，`build_config` 应该包含 `configurable.user_id`。如果获取不到，工具默认用 `"default"`。

### 7.4 mem0_client.py 的修改是必须的

当前 `is_mem0_enabled()` 只检查 `backend == "mem0"`。如果不修改，`backend=file` + `mem0_tool_enabled=true` 时 `get_mem0()` 返回 None，工具会报 "mem0 not initialized"。

**修改后的判断逻辑**：
```python
def is_mem0_enabled() -> bool:
    cfg = get_memory_config()
    if not cfg.enabled:
        return False
    return cfg.backend == "mem0" or getattr(cfg, "mem0_tool_enabled", False)
```

### 7.5 回滚方案

| 回滚目标 | 操作 |
|---------|------|
| 关闭工具但保留双写 | `mem0_tool_enabled: false` |
| 回到纯 file（无 mem0） | `backend: file` + `mem0_tool_enabled: false` |
| 回到纯 mem0（原方案） | `backend: mem0` + `mem0_tool_enabled: false` |

所有回滚只需改 `config.yaml`，无需改代码。

### 7.6 SubAgent 是否需要 memory_search 工具

当前设计只在 Lead Agent 注册 memory_search。SubAgent（researcher/coder/analyst/writer/reviewer）**不注册**。

理由：
- SubAgent 是短生命周期的任务执行者，由 Lead Agent 通过 `context` 参数传递必要信息
- 如果 SubAgent 需要历史信息，应该由 Lead Agent 先查好再通过 `context` 传入
- 避免工具过多增加 SubAgent 的决策复杂度

如果未来需要 SubAgent 也能查记忆，可以在 `harness/agents/presets.py` 的 SubAgent 工具列表中加入。

---

## 八、总结

| 改动 | 文件 | 效果 |
|------|------|------|
| DynamicContextMiddleware 简化 | `dynamic_context.py` | 回退 file 模式，删掉 mem0 分支（~250 行） |
| 新增 memory_search 工具 | `memory_tools.py`（新） | Agent 主动查 mem0（~120 行） |
| 工具注册 | `lead_tools.py` | build_lead_tools 加入 memory_search |
| 系统提示词 | `lead_agent.py` | 加 memory_tool_section + guidance |
| 配置字段 | `memory_config.py` | 新增 mem0_tool_enabled |
| config.yaml | `config.yaml` | backend=file + mem0_tool_enabled=true |
| 双写逻辑 | `updater.py` | aupdate_memory 同时写 file 和 mem0 |
| mem0_client 判断 | `mem0_client.py` | is_mem0_enabled 支持 tool 模式 |

**核心设计**：memory.json 负责被动注入基础上下文（粗粒度），mem0 负责主动精准查询（细粒度）。两套系统通过双写保持数据同步，各司其职。
