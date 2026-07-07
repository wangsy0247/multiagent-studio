"""Built-in tools required by the Lead Agent."""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from harness.agents.presets import PRESET_SUBAGENTS, build_subagent_config
from harness.tools.builtins.memory_tools import create_memory_search_tool

logger = logging.getLogger(__name__)


def create_subagent_tool(manager: Any | None = None) -> BaseTool:
    """Create the ``create_subagent`` tool used by the Lead Agent."""

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


def _extract_parent_state(
    config: RunnableConfig | None = None,
    state: dict | None = None,
) -> dict | None:
    """Extract parent context for SubAgent inheritance.

    Merges two sources:
    1. ``config`` (RunnableConfig auto-injected by LangChain) — provides
       ``thread_id`` from ``configurable``.
    2. ``state`` (graph state via InjectedState) — provides ``user_id``,
       ``thread_data`` path mappings, and ``workspace`` path.

    Returns None when no meaningful inheritance data is available.
    """
    result: dict[str, Any] = {}

    # ── from RunnableConfig: thread identity ──
    if config is not None:
        cfg = config.get("configurable", {}) or {}
        thread_id = cfg.get("thread_id", "")
        if thread_id:
            result["thread_id"] = thread_id

    # ── from graph state: user_id + path mappings (DeerFlow-aligned) ──
    if state is not None:
        # user_id is NOT in config.configurable — extract from state
        user_id = state.get("user_id")
        if user_id:
            result["user_id"] = user_id

        for key in ("thread_data", "workspace"):
            val = state.get(key)
            if val is not None:
                result[key] = val

    if not result:
        return None
    return result


def task_tool(
    manager: Any | None = None,
    *,
    parent_skills: list[str] | None = None,
) -> BaseTool:
    """Create the ``task`` tool used by the Lead Agent.

    Args:
        manager: SubagentManager instance.
        parent_skills: Optional parent skill allowlist to constrain subagent skills.
    """

    @tool
    async def task(
        agent_name: str,
        instruction: str,
        context: str = "",
        config: RunnableConfig = None,  # auto-injected by LangChain at call time
        state: Annotated[dict, InjectedState] = None,  # parent graph state (sandbox, thread_data)
    ) -> str:
        """Delegate a task to a SubAgent for execution.

        SubAgents have NO access to this conversation — your instruction
        must be fully self-contained. Before calling, think through:
        1. Goal — what exactly to deliver?
        2. Background — what context does the subagent need?
        3. Scope — which files / directories / line numbers?
        4. Constraints — READ-ONLY or allowed to modify which files?
        5. Format — what structure and length?

        Structure your instruction with these sections:
        【Goal】【Background】【Scope / Location】【Constraints】【Output Format】

        After the subagent returns, verify the actual output — do not
        blindly trust the summary. If code was modified, read the file.
        If facts were reported, spot-check key claims.

        Args:
            agent_name: Target SubAgent name (researcher / coder / analyst / writer / reviewer)
            instruction: Self-contained task specification following the template above.
            context: Additional background (prefer embedding this in instruction instead).
        """
        if manager is None:
            return "Error: SubAgent manager not initialized"
        try:
            parent_state = _extract_parent_state(config, state)
            result = await manager.execute(
                agent_name, instruction, context,
                parent_state=parent_state,
                parent_skills=parent_skills,
            )
            # ── 只返回 output 文本, 不暴露内部细节 ──
            # 内部消息 (ai_messages / token_usage_records) 通过 SubagentManager.
            # pop_last_result() → subagent_end SSE 事件 → 前端右侧详情面板获取。
            # Lead Agent 不接触这些细节, 避免主聊天流污染。
            output = result.output or ""
            if result.error:
                output = f"[{result.status}] {result.error}\n{output}"
            return output
        except Exception as exc:
            return f"Error: {exc}"

    return task


def ask_clarification_tool() -> BaseTool:
    """Create the ``ask_clarification`` tool used by the Lead Agent."""

    @tool(response_format="content", return_direct=True)
    def ask_clarification(
        question: str,
        clarification_type: Literal[
            "missing_info",
            "ambiguous_requirement",
            "approach_choice",
            "risk_confirmation",
            "suggestion",
        ],
        context: str | None = None,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user for clarification when you need more information to proceed.

        Use this tool when you encounter situations where you cannot proceed without user input:

        - **Missing information**: Required details not provided (e.g., file paths, URLs, specific requirements)
        - **Ambiguous requirements**: Multiple valid interpretations exist
        - **Approach choices**: Several valid approaches exist and you need user preference
        - **Risky operations**: Destructive actions that need explicit confirmation (e.g., deleting files, modifying production)
        - **Suggestions**: You have a recommendation but want user approval before proceeding

        The execution will be interrupted and the question will be presented to the user.
        Wait for the user's response before continuing.

        When to use ask_clarification:
        - You need information that wasn't provided in the user's request
        - The requirement can be interpreted in multiple ways
        - Multiple valid implementation approaches exist
        - You're about to perform a potentially dangerous operation
        - You have a recommendation but need user approval

        Best practices:
        - Ask ONE clarification at a time for clarity
        - Be specific and clear in your question
        - Don't make assumptions when clarification is needed
        - For risky operations, ALWAYS ask for confirmation
        - After calling this tool, execution will be interrupted automatically

        Args:
            question: The clarification question to ask the user. Be specific and clear.
            clarification_type: The type of clarification needed (missing_info, ambiguous_requirement, approach_choice, risk_confirmation, suggestion).
            context: Optional context explaining why clarification is needed. Helps the user understand the situation.
            options: Optional list of choices (for approach_choice or suggestion types). Present clear options for the user to choose from.
        """
        return "Clarification request processed by middleware"

    return ask_clarification


def build_lead_tools(
    manager: Any | None = None,
    *,
    parent_skills: list[str] | None = None,
) -> list[BaseTool]:
    """Return all built-in tools required by the Lead Agent.

    Tools included:
    - create_subagent: Create specialized SubAgents
    - task: Delegate tasks to SubAgents
    - ask_clarification: Ask user for clarification
    - memory_search: Query mem0 long-term memory (if mem0_tool_enabled)
    """
    tools: list[BaseTool] = [
        create_subagent_tool(manager),
        task_tool(manager, parent_skills=parent_skills),
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
