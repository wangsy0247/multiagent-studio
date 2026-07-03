"""SubAgent — backward-compatible thin wrapper around SubagentExecutor.

Prefer using ``SubagentExecutor`` directly for new code.  This module is kept
for backward compatibility with code that instantiates ``SubAgent`` and calls
``execute()`` directly.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from harness.agents.subagent_executor import SubagentExecutor
from harness.agents.subagent_middleware import build_subagent_middlewares
from harness.models import HarnessState, SubAgentConfig, SubAgentResult

logger = logging.getLogger(__name__)


class SubAgent:
    """Legacy SubAgent — delegates to ``SubagentExecutor`` internally.

    Deprecated: use ``SubagentExecutor`` for new integrations.  This class
    exists solely for backward compatibility and will be removed in v2.
    """

    def __init__(
        self,
        config: SubAgentConfig,
        llm: BaseChatModel,
        tools: list[BaseTool],
        middlewares: list[Any] | None = None,
    ):
        self.config = config
        self.llm = llm
        self.tools = tools
        # middlewares parameter is ignored — the executor uses its own
        # stripped-down middleware chain via build_subagent_middlewares()
        if middlewares:
            logger.debug(
                "SubAgent '%s': middlewares parameter is deprecated and ignored. "
                "SubagentExecutor uses a stripped-down middleware chain automatically.",
                config.name,
            )

    async def execute(
        self,
        instruction: str,
        context: str = "",
        parent_state: HarnessState | None = None,
    ) -> SubAgentResult:
        """Execute via SubagentExecutor in a thread (backward-compatible).

        Uses ``execute()`` (sync, on isolated loop) wrapped in
        ``asyncio.to_thread`` to avoid blocking the event loop.
        """
        import asyncio

        full_instruction = instruction
        if context:
            full_instruction = f"[上下文]\n{context}\n\n[任务]\n{instruction}"

        executor = SubagentExecutor(
            config=self.config,
            llm=self.llm,
            tools=self.tools,
            parent_state=parent_state,
        )
        return await asyncio.to_thread(executor.execute, full_instruction)
