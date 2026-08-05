"""DanglingToolCallMiddleware — detect and repair unmatched tool_calls.

Implements ``awrap_model_call`` — innermost onion wrapper closest to LLM.
Patches message history before the model sees it, following harness pattern.
"""
from __future__ import annotations

import logging
from typing import Any, override

from langchain_core.messages import AIMessage, ToolMessage

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)


class DanglingToolCallMiddleware(HarnessAgentMiddleware):
    """Detect tool_calls never answered by a ToolMessage and inject synthetic errors.

    Runs as the innermost ``awrap_model_call`` wrapper (onion) — the patched
    message list is passed to the real LLM call.
    """

    name = "dangling_tool_call"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    # ------------------------------------------------------------------
    # awrap_model_call — onion model, innermost wrapper
    # ------------------------------------------------------------------

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Patch dangling tool calls before passing to handler (real LLM)."""
        # request has .messages (list of LangChain messages)
        messages = getattr(request, "messages", None)
        if messages is None:
            return await handler(request)

        patched = self._build_patched_messages(list(messages))
        if patched is not None:
            # Use request.override() if available (harness pattern)
            if hasattr(request, "override"):
                request = request.override(messages=patched)
            else:
                request.messages = patched

        return await handler(request)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def _build_patched_messages(self, messages: list) -> list | None:
        """Scan for dangling tool_calls and inject synthetic ToolMessages.
        Returns patched list or None if no repair needed."""
        # Collect all tool_call ids
        tool_call_ids: dict[str, int] = {}  # id -> position in AIMessage
        for i, msg in enumerate(messages):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_ids[tc["id"]] = i

        # Collect responded ids
        responded: set[str] = set()
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                responded.add(msg.tool_call_id)

        # Find dangling (unresponded) ids
        dangling = [tid for tid in tool_call_ids if tid not in responded]
        if not dangling:
            return None

        logger.warning("Found %d dangling tool call(s), injecting synthetic responses", len(dangling))

        # Build patched list: insert synthetic ToolMessage after each dangling AIMessage
        patched: list = []
        for msg in messages:
            patched.append(msg)
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc["id"] in dangling:
                        patched.append(ToolMessage(
                            content="[工具执行被中断，未返回结果]",
                            tool_call_id=tc["id"],
                            name=tc.get("name", "unknown"),
                            status="error",
                        ))

        return patched
