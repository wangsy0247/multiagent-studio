"""Middleware base class — HarnessAgentMiddleware extending LangChain's AgentMiddleware.

Provides the base class for all 14 Harness middleware, compatible with
``create_agent()`` from ``langchain.agents``.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from harness.models import HarnessState

logger = logging.getLogger(__name__)


class HarnessAgentMiddleware(AgentMiddleware[HarnessState, Any, Any]):
    """Base class for all Harness middleware, extending LangChain's AgentMiddleware.

    Parameters
    ----------
    config : dict | None
        Configuration dict.

    Hooks (override in subclasses)
    -------------------------------
    *async* variants are the primary interface.

    Per-turn hooks (one invocation per ``create_agent()`` call):
      - ``abefore_agent(state, runtime)``   → dict | None
      - ``aafter_agent(state, runtime)``    → dict | None

    Per-model-call hooks (one invocation per LLM call):
      - ``abefore_model(state, runtime)``   → dict | None
      - ``aafter_model(state, runtime)``    → dict | None

    Wrap hooks (intercept model / tool execution):
      - ``awrap_model_call(request, handler)`` → ModelResponse
      - ``awrap_tool_call(request, handler)``  → ToolMessage | Command

    Deferred hooks (for sub-agent lifecycle):
      - ``defer_before(state, runtime)`` → None
      - ``defer_after(state, runtime)``  → None
    """

    state_schema: type[HarnessState] = HarnessState
    """Use HarnessState as the agent state schema for create_agent()."""

    name: str = "harness_base"
    """Human-readable name shown in middleware lists."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.config = config or {}
        logger.debug("Initialized %s with config=%s", self.name, self.config)

    # ------------------------------------------------------------------
    # Per-turn hooks
    # ------------------------------------------------------------------

    async def abefore_agent(
        self, state: HarnessState, runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Called once before the agent starts executing.  Return state updates or None."""
        return None

    async def aafter_agent(
        self, state: HarnessState, runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Called once after the agent finishes.  Return state updates or None."""
        return None

    # ------------------------------------------------------------------
    # Per-model-call hooks
    # ------------------------------------------------------------------

    async def abefore_model(
        self, state: HarnessState, runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Called before each LLM call.  Return state updates or None."""
        return None

    async def aafter_model(
        self, state: HarnessState, runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Called after each LLM call.  Return state updates or None."""
        return None

    # ------------------------------------------------------------------
    # Wrap hooks
    # ------------------------------------------------------------------

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Wrap individual tool execution.  Call ``await handler(request)`` to invoke."""
        return await handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Wrap model execution.  Call ``await handler(request)`` to invoke."""
        return await handler(request)

    # ------------------------------------------------------------------
    # Deferred hooks (sub-agent lifecycle)
    # ------------------------------------------------------------------

    async def defer_before(self, state: HarnessState, runtime: Runtime[Any]) -> None:
        """Called before a SubAgent starts."""
        pass

    async def defer_after(self, state: HarnessState, runtime: Runtime[Any]) -> None:
        """Called after a SubAgent finishes."""
        pass

    # ------------------------------------------------------------------
    # Sync hooks — removed overrides; let parent AgentMiddleware no-ops handle them.
    # LangChain 1.x calls sync hooks before async ones; raising NotImplementedError
    # here breaks the middleware chain.  Async hooks (abefore_*, aafter_*) above
    # contain all the actual logic.
    # ------------------------------------------------------------------
