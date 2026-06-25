"""Lead Agent — multi-agent orchestration core with DeerFlow-style prompt.

The LeadAgent is a *configuration provider* for ``create_agent()`` — it builds
the system prompt and tool list.  The actual ReAct loop is handled by
``create_agent()`` internally, wrapped by the outer ``HarnessGraphFactory``.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from harness.agents.presets import PRESET_SUBAGENTS, build_subagent_config
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
    # tools — built once and passed to create_agent()
    # ------------------------------------------------------------------

    def _create_subagent_tool(self) -> BaseTool:
        manager = self.subagent_manager

        @tool
        async def create_subagent(
            name: str,
            agent_type: Literal["researcher", "coder", "analyst", "writer", "reviewer"],
            description: str = "",
            custom_system_prompt: str = "",
        ) -> str:
            """Create a new SubAgent to handle a specific task.

            Args:
                name: SubAgent name (English, used for identification)
                agent_type: Preset type determining pre-configured capabilities
                description: What this subagent is responsible for
                custom_system_prompt: Custom system prompt (overrides preset)
            """
            if manager is None:
                return "Error: SubAgent manager not initialized"
            config = build_subagent_config(
                name=name,
                agent_type=agent_type,
                description=description,
                custom_system_prompt=custom_system_prompt,
            )
            try:
                await manager.create(config)
            except ValueError as exc:
                return str(exc)
            return f"SubAgent '{name}' ({agent_type}) created successfully"

        return create_subagent

    def _task_tool(self) -> BaseTool:
        manager = self.subagent_manager

        @tool
        async def task(
            agent_name: str,
            instruction: str,
            context: str = "",
        ) -> str:
            """Delegate a task to a SubAgent for execution.

            SubAgents are independent agents with their own tools and context.
            Use this to parallelize complex work across specialized agents.

            Args:
                agent_name: Target SubAgent name
                instruction: Detailed task instruction with acceptance criteria
                context: Additional background information
            """
            if manager is None:
                return "Error: SubAgent manager not initialized"
            try:
                result = await manager.execute(agent_name, instruction, context)
                import json
                return json.dumps(result.model_dump(), ensure_ascii=False)
            except Exception as exc:
                return f"Error: {exc}"

        return task

    def _ask_clarification_tool(self) -> BaseTool:
        @tool
        def ask_clarification(
            question: str,
            context: str = "",
            clarification_type: str = "missing_info",
            options: list[str] | None = None,
        ) -> str:
            """Ask the user for clarification before proceeding.

            Args:
                question: The specific question to ask
                context: Why this clarification is needed
                clarification_type: missing_info / ambiguous_requirement / approach_choice / risk_confirmation
                options: Optional list of choices to present
            """
            return f"[Awaiting user confirmation] {question}"

        return ask_clarification

    # ------------------------------------------------------------------
    # system prompt
    # ------------------------------------------------------------------

    def get_system_prompt(self) -> str:
        """Build the complete system prompt using the DeerFlow-style template."""
        return apply_prompt_template(
            agent_name=self.agent_name,
            max_concurrent_subagents=self.max_concurrent,
            subagent_enabled=self.subagent_manager is not None,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def build_tools(self) -> list[BaseTool]:
        """Return all tools for the Lead Agent.

        Called by ``HarnessService`` to pass to ``create_agent()``.
        """
        tools: list[BaseTool] = [
            self._create_subagent_tool(),
            self._task_tool(),
            self._ask_clarification_tool(),
        ]
        try:
            tools.extend(self.tool_registry.get_core_tools())
        except Exception:
            pass
        return tools
