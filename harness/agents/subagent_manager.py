"""SubAgent Manager — lifecycle, registry, and concurrency control."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel

from harness.agents.subagent import SubAgent
from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState, SubAgentConfig, SubAgentResult
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class SubagentManager:
    """Manage SubAgent creation, lookup, execution, and teardown.

    Concurrency is controlled via an ``asyncio.Semaphore`` whose size is
    clamped to [2, 4].  Each SubAgent receives a copy of the middleware
    list (as ``AgentMiddleware`` instances) for its own ``create_agent()``
    invocation.
    """

    def __init__(
        self,
        llm_factory: Callable[[str], BaseChatModel],
        tool_registry: ToolRegistry,
        middlewares: list[HarnessAgentMiddleware] | None = None,
        max_concurrent: int = 3,
    ):
        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._middlewares: list[HarnessAgentMiddleware] = middlewares or []
        self._max_concurrent: int = min(max(int(max_concurrent), 2), 4)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._agents: dict[str, SubAgent] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, config: SubAgentConfig, parent_model: str | None = None) -> SubAgent:
        """Create and register a new SubAgent.

        - If ``config.tools`` is None, all registry tools are inherited.
        - If ``config.model`` is 'inherit', ``parent_model`` is used.
        """
        if config.name in self._agents:
            raise ValueError(f"SubAgent '{config.name}' 已存在")

        # Resolve model
        model_name = config.model
        if model_name == "inherit":
            model_name = parent_model or "gpt-4o"
        llm = self._llm_factory(model_name)

        # Resolve tools
        if config.tools is None:
            tools = self._tool_registry.get_core_tools()
        else:
            tools = [
                self._tool_registry.get_tool(name)
                for name in config.tools
                if self._tool_registry.has_tool(name)
            ]

        agent = SubAgent(
            config=config,
            llm=llm,
            tools=tools,
            middlewares=self._middlewares,
        )
        self._agents[config.name] = agent
        logger.info("SubAgent created: name=%s type=%s", config.name, config.display_name)
        return agent

    def get(self, name: str) -> SubAgent | None:
        """Look up a SubAgent by name."""
        return self._agents.get(name)

    def list(self) -> list[SubAgentConfig]:
        """Return configs for all registered SubAgents."""
        return [a.config for a in self._agents.values()]

    async def delete(self, name: str) -> None:
        """Remove a SubAgent."""
        self._agents.pop(name, None)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        instruction: str,
        context: str = "",
        parent_state: HarnessState | None = None,
    ) -> SubAgentResult:
        """Dispatch a task to a SubAgent with concurrency gating."""
        agent = self._agents.get(name)
        if agent is None:
            return SubAgentResult(status="error", output=f"SubAgent '{name}' 不存在")

        async with self._semaphore:
            return await agent.execute(instruction, context, parent_state)
