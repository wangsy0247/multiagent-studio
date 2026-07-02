"""Lead Agent — multi-agent orchestration core with DeerFlow-style prompt.

The LeadAgent is a *configuration provider* for ``create_agent()`` — it builds
the system prompt and tool list.  The actual ReAct loop is handled by
``create_agent()`` internally, wrapped by the outer ``HarnessGraphFactory``.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from harness.agents.presets import PRESET_SUBAGENTS
from harness.tools.builtins import build_lead_tools
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Prompt template — DeerFlow-style XML-structured system prompt
# ──────────────────────────────────────────────────────────────────────────────


def _build_subagent_section(max_concurrent: int) -> str:
    """Build the subagent system prompt section with dynamic concurrency limit."""
    n = max_concurrent
    agent_descriptions = "\n".join(
        f"- **{name}**: {info['display_name']} — {info['description']}"
        for name, info in PRESET_SUBAGENTS.items()
    )
    return f"""<subagent_system>
**🚀 SUBAGENT MODE ACTIVE — DECOMPOSE, DELEGATE, SYNTHESIZE**

You are running with subagent capabilities. Your role is to be a **task orchestrator**:
1. **DECOMPOSE**: Break complex tasks into parallel sub-tasks
2. **DELEGATE**: Launch multiple subagents simultaneously using parallel `task` calls
3. **SYNTHESIZE**: Collect and integrate results into a coherent answer

**⛔ HARD CONCURRENCY LIMIT: MAXIMUM {n} `task` CALLS PER RESPONSE.**
- Each response, you may include **at most {n}** `task` tool calls.
- Before launching subagents, COUNT your sub-tasks:
  - If count ≤ {n}: Launch all in this response.
  - If count > {n}: Pick the {n} most important for this turn.

**Available Subagents:**
{agent_descriptions}

✅ **USE SubAgents when:**
- Complex research questions requiring multiple information sources
- Multi-aspect analysis with several independent dimensions
- Large codebases needing parallel analysis
- Tasks that benefit from isolated context and specialized tools

❌ **Execute DIRECTLY when:**
- Task cannot be decomposed into 2+ meaningful parallel sub-tasks
- Ultra-simple actions: read one file, quick edits, single commands
- Need immediate user clarification
- Sequential dependencies where each step depends on previous results

**CRITICAL: Max {n} `task` calls per turn. For >{n} sub-tasks, use sequential batches.**
</subagent_system>"""


def _build_clarification_section() -> str:
    return """<clarification_system>
**WORKFLOW PRIORITY: CLARIFY → PLAN → ACT**

1. **FIRST**: Analyze — identify what's unclear, missing, or ambiguous
2. **SECOND**: If clarification needed, call `ask_clarification` IMMEDIATELY
3. **THIRD**: Only after all clarifications are resolved, proceed with execution

**CRITICAL RULE: Clarification ALWAYS comes BEFORE action.**

**MANDATORY Clarification Scenarios:**
- **Missing Information**: Required details not provided
- **Ambiguous Requirements**: Multiple valid interpretations exist
- **Approach Choices**: Several valid approaches exist
- **Risky Operations**: Destructive actions needing confirmation

**STRICT ENFORCEMENT:**
- ❌ Do NOT start working and then ask for clarification mid-execution
- ❌ Do NOT make assumptions when information is missing
- ✅ Analyze → Identify unclear aspects → Ask BEFORE any action
</clarification_system>"""


def _build_working_directory_section() -> str:
    return """<working_directory>
- User uploads: `{workspace}/uploads` — Files uploaded by the user
- User workspace: `{workspace}` — Working directory for temporary files
- Output files: `{workspace}/outputs` — Final deliverables

**File Management:**
- All temporary work happens in the workspace
- Prefer relative paths for scripts and commands
- Final deliverables should be clearly identified
</working_directory>"""


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


def _build_response_style_section() -> str:
    return """<response_style>
- Clear and Concise: Avoid over-formatting unless requested
- Natural Tone: Use paragraphs and prose, not bullet points by default
- Action-Oriented: Focus on delivering results
- Language Consistency: Keep using the same language as the user
- Always Respond: Your thinking is internal. You MUST always provide a visible response.
</response_style>"""


def _build_critical_reminders_section(max_concurrent: int) -> str:
    return f"""<critical_reminders>
- **Clarification First**: ALWAYS clarify unclear requirements BEFORE starting work
- **Orchestrator Mode**: Decompose complex tasks into parallel sub-tasks. **HARD LIMIT: max {max_concurrent} `task` calls per response.**
- Multi-task: Better utilize parallel tool calling for better performance
- Language Consistency: Keep using the same language as user's
- Always Respond: You MUST always provide a visible response to the user after thinking
</critical_reminders>"""


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


# ──────────────────────────────────────────────────────────────────────────────
# LeadAgent class
# ──────────────────────────────────────────────────────────────────────────────


class LeadAgent:
    """Lead Agent — configuration provider for ``create_agent()``.

    Builds the system prompt and tool list.  The actual ReAct execution loop
    is handled by ``create_agent()`` (via ``HarnessGraphFactory``), so there
    is no ``run()`` method — the agent is driven entirely by the graph.

    Responsibilities:
    - Understand user intent and plan task decomposition
    - Decide when to create SubAgents and what type
    - Orchestrate SubAgent execution order (serial / parallel)
    - Integrate SubAgent results into a coherent final answer
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        subagent_manager: Any | None = None,
        agent_name: str = "Multi-Agent Orchestrator",
        max_concurrent_subagents: int = 3,
    ):
        self.tool_registry = tool_registry
        self.subagent_manager = subagent_manager
        self.agent_name = agent_name
        self.max_concurrent = max_concurrent_subagents

    # ------------------------------------------------------------------
    # system prompt
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def build_tools(self) -> list[BaseTool]:
        """Return all tools for the Lead Agent.

        Called by ``HarnessService`` to pass to ``create_agent()``.
        """
        tools: list[BaseTool] = build_lead_tools(self.subagent_manager)
        try:
            tools.extend(self.tool_registry.get_core_tools())
        except Exception:
            pass
        return tools


# ──────────────────────────────────────────────────────────────────────────────
# Middleware builder + app factory (added during DeerFlow-aligned refactor)
# ──────────────────────────────────────────────────────────────────────────────

from harness.config.config_manager import ConfigManager
from harness.middleware.clarification import ClarificationMiddleware
from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.middleware.guardrail import GuardrailMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.summarization import create_summarization_middleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.title import TitleMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.uploads import UploadsMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.sandbox import SandboxMiddleware
from harness.models import HarnessState
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig


def _get_runtime_config(config: RunnableConfig) -> dict:
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg


def _build_middlewares(
    config: RunnableConfig,
    *,
    config_manager: ConfigManager | None = None,
    agent_name: str | None = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
) -> list[AgentMiddleware]:
    """Build the full 20-middleware chain matching DeerFlow order."""
    middlewares: list[AgentMiddleware] = []
    cfg = _get_runtime_config(config)
    workspace_root = str(cfg.get("workspace_root", ""))
    is_plan_mode = cfg.get("is_plan_mode", False)
    subagent_enabled = cfg.get("subagent_enabled", False)
    max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
    memory_enabled = True; summarization_enabled = False; guardrail_enabled = False
    vision_enabled = cfg.get("vision_enabled", False)
    tool_search_enabled = cfg.get("tool_search_enabled", False)
    tool_max_retries = cfg.get("tool_max_retries", 3)
    auto_title_enabled = cfg.get("auto_title", False)
    title_model = cfg.get("title_model", "gpt-4o-mini")
    api_key = cfg.get("openai_api_key", ""); base_url = cfg.get("openai_base_url", "")
    if config_manager is not None:
        try:
            mem_cfg = config_manager.get("memory")
            if isinstance(mem_cfg, dict): memory_enabled = mem_cfg.get("enabled", True)
        except Exception: pass
        try:
            summ_cfg = config_manager.get("summarization")
            if isinstance(summ_cfg, dict): summarization_enabled = summ_cfg.get("enabled", False)
        except Exception: pass
        try:
            guard_cfg = config_manager.get("guardrails")
            if isinstance(guard_cfg, dict): guardrail_enabled = guard_cfg.get("enabled", False)
        except Exception: pass
        try:
            ts_cfg = config_manager.get("tool_search")
            if isinstance(ts_cfg, dict): tool_search_enabled = ts_cfg.get("enabled", False)
        except Exception: pass
    middlewares.append(ThreadDataMiddleware({"workspace_root": workspace_root}))
    middlewares.append(UploadsMiddleware())
    middlewares.append(SandboxMiddleware())
    middlewares.append(DanglingToolCallMiddleware())
    middlewares.append(LLMErrorHandlingMiddleware())
    if guardrail_enabled: middlewares.append(GuardrailMiddleware())
    middlewares.append(SandboxAuditMiddleware())
    middlewares.append(ToolErrorHandlingMiddleware({"max_retries": tool_max_retries}))
    middlewares.append(DynamicContextMiddleware(agent_name=agent_name))
    if summarization_enabled:
        from harness.memory.summarization_hook import memory_flush_hook
        hooks = [memory_flush_hook] if memory_enabled else []
        summ_mw = create_summarization_middleware(before_summarization=hooks)
        if summ_mw is not None: middlewares.append(summ_mw)
    if is_plan_mode: middlewares.append(TodoMiddleware())
    middlewares.append(TokenUsageMiddleware())
    if auto_title_enabled:
        middlewares.append(TitleMiddleware({"title_model": title_model, "api_key": api_key, "base_url": base_url}))
    if memory_enabled: middlewares.append(MemoryMiddleware(agent_name=agent_name))
    if vision_enabled: middlewares.append(ViewImageMiddleware())
    if tool_search_enabled: middlewares.append(DeferredToolFilterMiddleware())
    if subagent_enabled: middlewares.append(SubagentLimitMiddleware({"max_concurrent": max_concurrent_subagents}))
    middlewares.append(LoopDetectionMiddleware())
    middlewares.append(SafetyFinishReasonMiddleware())
    if custom_middlewares: middlewares.extend(custom_middlewares)
    middlewares.append(ClarificationMiddleware())
    return middlewares


def make_lead_agent(config: RunnableConfig, *, config_manager: ConfigManager | None = None):
    """LangGraph graph factory from config.yaml."""
    cfg = _get_runtime_config(config)
    agent_name = cfg.get("agent_name")
    model_name = cfg.get("model_name") or cfg.get("model", "gpt-4o")
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=model_name, api_key=cfg.get("openai_api_key", ""),
                     base_url=cfg.get("openai_base_url", ""), temperature=0.3)
    tools = cfg.get("_tools", [])
    system_prompt = cfg.get("_system_prompt", "You are a helpful assistant.")
    middlewares = _build_middlewares(config, config_manager=config_manager, agent_name=agent_name)
    return create_agent(model=llm, tools=tools or None, middleware=middlewares,
                        system_prompt=system_prompt, state_schema=HarnessState)
