"""DeferredToolFilterMiddleware — hide deferred MCP tool schemas from model binding.

DeerFlow-aligned: when tool_search is enabled, MCP tool schemas are hidden
from the LLM until promoted via the ``tool_search`` tool.  Promotions live
in the graph state (``promoted_tools``), merged by the
``merge_promoted_tools`` reducer with catalog-hash drift protection.

- ``awrap_model_call``: filter (deferred − promoted) schemas from request.tools
- ``awrap_tool_call``: block execution of any deferred tool not yet promoted

The deferred set itself comes from ``harness.tools.tool_search.get_deferred_setup()``;
when no setup exists (feature disabled / below threshold) this middleware is a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware
from harness.tools.tool_search import get_deferred_setup, promoted_names_from_state

logger = logging.getLogger(__name__)


class DeferredToolFilterMiddleware(HarnessAgentMiddleware):
    """Filter deferred tools from model binding and block unpromoted tool calls."""

    name = "deferred_tool_filter"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hidden_names(state: Any) -> frozenset[str]:
        """deferred − promoted (catalog_hash 漂移时 promoted 视为全部失效)."""
        setup = get_deferred_setup()
        if setup is None:
            return frozenset()
        return setup.deferred_names - promoted_names_from_state(state)

    # ------------------------------------------------------------------
    # awrap_model_call — filter schemas from model binding
    # ------------------------------------------------------------------

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        if hasattr(request, "tools") and hasattr(request, "state"):
            hidden = self._hidden_names(getattr(request, "state", None))
            if hidden:
                active_tools = [
                    t for t in request.tools
                    if getattr(t, "name", None) not in hidden
                ]
                if len(active_tools) < len(request.tools):
                    logger.debug(
                        "Filtered %d deferred tool schema(s) from model binding",
                        len(request.tools) - len(active_tools),
                    )
                    request = request.override(tools=active_tools)
        return await handler(request)

    # ------------------------------------------------------------------
    # awrap_tool_call — block unpromoted deferred tools
    # ------------------------------------------------------------------

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = str(request.tool_call.get("name", ""))
        hidden = self._hidden_names(getattr(request, "state", None))

        if tool_name in hidden:
            tool_call_id = str(request.tool_call.get("id", "missing_id"))
            logger.warning("Blocked unpromoted deferred tool: %s", tool_name)
            return ToolMessage(
                content=(
                    f"Tool '{tool_name}' is deferred and has not been promoted yet. "
                    f"Call tool_search first to load its schema."
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
                status="error",
            )

        return await handler(request)
