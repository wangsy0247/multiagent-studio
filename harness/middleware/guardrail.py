"""GuardrailMiddleware — tool-call authorization via awrap_tool_call.

Matches the standard GuardrailMiddleware design: wraps each tool call and
evaluates it against a GuardrailProvider before execution. Denied calls
return an error ToolMessage so the agent can adapt.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)


class GuardrailMiddleware(HarnessAgentMiddleware):
    """Evaluate each tool call against a permission policy before execution.

    Supports allow-list and deny-list with a configurable default policy
    (``allow`` or ``deny``).

    Parameters
    ----------
    config : dict
        ``tool_permissions`` — per-agent-type permission dict::
            {"default": {"deny_list": ["bash"], "allow_list": None}}
        ``default_policy`` — ``"allow"`` (default) or ``"deny"``.
    """

    name = "guardrail"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._permissions: dict = self.config.get("tool_permissions", {})
        self._default_policy: str = self.config.get("default_policy", "allow")

    # ------------------------------------------------------------------
    # permission check
    # ------------------------------------------------------------------

    def _is_allowed(self, tool_name: str, agent_type: str = "default") -> bool:
        perms = self._permissions.get(agent_type, {})
        deny_list: list[str] = list(perms.get("deny_list", []))
        allow_list: list[str] | None = perms.get("allow_list")

        if tool_name in deny_list:
            return False
        if allow_list is not None and tool_name not in allow_list:
            return False
        return self._default_policy == "allow"

    def _build_denied_message(self, request: ToolCallRequest, tool_name: str) -> ToolMessage:
        tool_call_id = str(request.tool_call.get("id", "missing_id"))
        return ToolMessage(
            content=(
                f"Guardrail denied: tool '{tool_name}' is blocked by the "
                f"current permission policy. Choose an alternative approach."
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    # ------------------------------------------------------------------
    # awrap_tool_call — the core hook
    # ------------------------------------------------------------------

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = str(request.tool_call.get("name", "unknown_tool"))

        try:
            allowed = self._is_allowed(tool_name)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("Guardrail permission check failed for tool=%s", tool_name)
            # fail-closed: block on error
            return self._build_denied_message(request, tool_name)

        if not allowed:
            logger.warning("Guardrail denied: tool=%s", tool_name)
            return self._build_denied_message(request, tool_name)

        return await handler(request)
