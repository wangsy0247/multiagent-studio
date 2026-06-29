"""DeferredToolFilterMiddleware — hide deferred MCP tool schemas from model binding.

Matches DeerFlow's design: when tool_search is enabled, MCP tools are
registered in a deferred registry but their schemas are hidden from the
LLM until explicitly promoted via the ``tool_search`` tool.

- ``awrap_model_call``: filter deferred tools from request.tools
- ``awrap_tool_call``: block execution of any deferred tool not yet promoted
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)

# Simple in-memory deferred tool registry (can be replaced with a more
# sophisticated registry later — for now matches the concept).
_deferred_tool_names: set[str] = set()
_promoted_tool_names: dict[str, set[str]] = {}  # thread_id → set of promoted tool names


def register_deferred_tool(name: str) -> None:
    """Register a tool name in the deferred registry."""
    _deferred_tool_names.add(name)


def is_deferred_tool(name: str) -> bool:
    """Return True if this tool is in the deferred registry."""
    return name in _deferred_tool_names


def promote_deferred_tool(thread_id: str, name: str) -> None:
    """Promote a deferred tool so it can be executed."""
    if thread_id not in _promoted_tool_names:
        _promoted_tool_names[thread_id] = set()
    _promoted_tool_names[thread_id].add(name)


def is_promoted(thread_id: str, name: str) -> bool:
    """Return True if this tool has been promoted for this thread."""
    return thread_id in _promoted_tool_names and name in _promoted_tool_names[thread_id]


class DeferredToolFilterMiddleware(HarnessAgentMiddleware):
    """Filter deferred tools from model binding and block unpromoted tool calls.

    - ``awrap_model_call``: removes deferred tool schemas from request.tools
    - ``awrap_tool_call``: blocks execution of deferred tools not yet promoted
    """

    name = "deferred_tool_filter"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    # ------------------------------------------------------------------
    # awrap_model_call — filter schemas from model binding
    # ------------------------------------------------------------------

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        if hasattr(request, "tools") and _deferred_tool_names:
            active_tools = [
                t for t in request.tools
                if getattr(t, "name", None) not in _deferred_tool_names
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

        if is_deferred_tool(tool_name):
            # Resolve thread_id from request context (best effort)
            thread_id = getattr(request, "thread_id", "default")
            if not is_promoted(str(thread_id), tool_name):
                tool_call_id = str(request.tool_call.get("id", "missing_id"))
                logger.warning("Blocked unpromoted deferred tool: %s", tool_name)
                return ToolMessage(
                    content=(
                        f"Tool '{tool_name}' is not available. "
                        f"Use the tool_search tool to discover and enable it first."
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    status="error",
                )

        return await handler(request)
