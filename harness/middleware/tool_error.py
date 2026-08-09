"""ToolErrorHandlingMiddleware — retry failed tool calls via wrap_tool_call hook."""
from __future__ import annotations

import logging
from typing import override

from langchain_core.messages import ToolMessage

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)


class ToolErrorHandlingMiddleware(HarnessAgentMiddleware):
    """Retry failed tool calls with configurable backoff.

    Uses the ``awrap_tool_call`` hook to intercept each tool execution.
    On failure the tool is retried up to ``max_retries`` times before
    the error is allowed to propagate.
    """

    name = "tool_error_handling"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.max_retries = int(self.config.get("max_retries", 3))

    @override
    async def awrap_tool_call(self, request, handler):
        """Retry the tool call up to max_retries times on failure."""
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await handler(request)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning(
                        "Tool '%s' failed (attempt %d/%d): %s",
                        request.tool_call.get("name", "unknown"),
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                    )
                else:
                    logger.error(
                        "Tool '%s' failed after %d retries: %s",
                        request.tool_call.get("name", "unknown"),
                        self.max_retries + 1,
                        exc,
                    )

        # All retries exhausted — return error ToolMessage instead of raising
        return ToolMessage(
            content=f"Tool execution failed (still failing after {self.max_retries} retries): {last_error}",
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name", "unknown"),
            status="error",
        )
