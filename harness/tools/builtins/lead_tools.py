"""Built-in tools required by the Lead Agent."""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

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


def task_tool(manager: Any | None = None) -> BaseTool:
    """Create the ``task`` tool used by the Lead Agent."""

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
            return json.dumps(result.model_dump(), ensure_ascii=False)
        except Exception as exc:
            return f"Error: {exc}"

    return task


def ask_clarification_tool() -> BaseTool:
    """Create the ``ask_clarification`` tool used by the Lead Agent."""

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
