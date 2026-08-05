"""SandboxMiddleware — bind runtime context to sandbox-backed tools.

With the harness-style sandbox provider layer, this middleware no longer
creates Docker containers directly. Instead it injects the current thread and
workspace into the sandbox tool context so those tools can acquire the
configured sandbox provider on demand.
"""
from __future__ import annotations

import logging
from typing import Any, override

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState
from harness.tools.sandbox_tools import set_sandbox_tool_context

logger = logging.getLogger(__name__)


def _set_context_from_state(state: Any) -> None:
    """Extract thread_id/workspace/user_id from state and set tool contexts."""
    if isinstance(state, dict):
        thread_id = state.get("thread_id", "default")
        workspace = state.get("workspace", ".")
        user_id = state.get("user_id")
    else:
        thread_id = getattr(state, "thread_id", "default")
        workspace = getattr(state, "workspace", ".")
        user_id = getattr(state, "user_id", None)
    set_sandbox_tool_context(workspace=workspace, thread_id=thread_id, user_id=user_id)


class SandboxMiddleware(HarnessAgentMiddleware):
    """Inject thread/workspace context into sandbox-backed tools."""

    name = "sandbox"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Set sandbox context for file/shell tools at agent start."""
        _set_context_from_state(state)
        logger.debug("Sandbox context set for agent run")
        return None

    @override
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Re-inject sandbox context before each tool call.

        ``abefore_agent`` only runs once per agent turn, but individual tool
        calls may execute in a different async context. Re-setting the context
        here guarantees that sandbox tools see the correct thread_id and
        workspace.
        """
        state = getattr(request, "state", None)
        if state is None and isinstance(request, dict):
            state = request.get("state")
        _set_context_from_state(state)

        return await handler(request)
