"""Middleware base class — HarnessAgentMiddleware extending LangChain's AgentMiddleware.

Provides a thin project-specific base class for all Harness middleware, compatible
with ``create_agent()`` from ``langchain.agents``.

Important: this class intentionally does **not** override any async hook defaults.
``create_agent()`` decides which middleware nodes to add to the graph by checking
whether a subclass overrides a hook. If we provided default async implementations
here, every subclass would appear to implement every hook and the graph would get
needlessly deep (one node per hook per middleware). Subclasses should override only
the hooks they actually need.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from harness.models import HarnessState

logger = logging.getLogger(__name__)


class HarnessAgentMiddleware(AgentMiddleware[HarnessState, Any, Any]):
    """Base class for all Harness middleware, extending LangChain's AgentMiddleware.

    Parameters
    ----------
    config : dict | None
        Configuration dict passed down from ``_register_middlewares()``.

    Hooks (override in subclasses only when needed)
    ------------------------------------------------
    *Async* variants are the primary interface because the Harness service runs
    entirely asynchronously.

    Per-turn hooks (one invocation per agent run):
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
