"""ClarificationMiddleware — human-in-the-loop confirmation and approval.

Aligned with DeerFlow's approach: intercept ``ask_clarification`` tool calls
before they execute and return a ``Command`` that jumps to ``END`` with the
formatted question stored as a ``ToolMessage``. This cleanly stops the ReAct
loop and lets the frontend present the clarification request.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import ClarificationRequest, HarnessState

logger = logging.getLogger(__name__)


class ClarificationMiddleware(HarnessAgentMiddleware):
    """Intercept ``ask_clarification`` tool calls and create a pending request.

    - ``abefore_agent``: if a pending clarification has been answered, inject
      the answer into the message history.
    - ``awrap_tool_call``: detect ``ask_clarification`` before it runs and
      interrupt execution with a ``Command(goto=END)``.
    """

    name = "clarification"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._asked: dict[str, set[str]] = {}

    def cleanup_thread(self, thread_id: str) -> None:
        """Remove per-thread state to prevent memory leaks (#9)."""
        self._asked.pop(thread_id, None)

    def _stable_message_id(self, tool_call_id: str, formatted_message: str) -> str:
        """Build a deterministic message ID so retries replace, not append."""
        if tool_call_id:
            return f"clarification:{tool_call_id}"
        digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
        return f"clarification:{digest}"

    def _is_chinese(self, text: str) -> bool:
        """Check if text contains Chinese characters."""
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    def _format_clarification_message(self, args: dict) -> str:
        """Format the clarification arguments into a user-friendly message."""
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        # Some models serialize array parameters as JSON strings instead of
        # native arrays. Deserialize and normalize so ``options`` is always a list.
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (json.JSONDecodeError, TypeError):
                options = [options]

        if options is None:
            options = []
        elif not isinstance(options, list):
            options = [options]

        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }
        icon = type_icons.get(clarification_type, "❓")

        message_parts = []
        if context:
            message_parts.append(f"{icon} {context}")
            message_parts.append(f"\n{question}")
        else:
            message_parts.append(f"{icon} {question}")

        if options:
            message_parts.append("")
            for i, option in enumerate(options, 1):
                message_parts.append(f"  {i}. {option}")

        return "\n".join(message_parts)

    def _build_request(self, args: dict) -> ClarificationRequest:
        """Create a ``ClarificationRequest`` from tool call arguments."""
        options = args.get("options", [])
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (json.JSONDecodeError, TypeError):
                options = [options]
        if options is None:
            options = []
        elif not isinstance(options, list):
            options = [options]

        return ClarificationRequest(
            id=str(uuid4()),
            question=args.get("question", "请确认"),
            context=args.get("context", ""),
            options=options,
            required=args.get("required", False),
            created_at=datetime.now(),
        )

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        """Interrupt execution and present the clarification question."""
        tool_call = request.tool_call
        args = tool_call.get("args", {})
        question = args.get("question", "请确认")
        tool_call_id = tool_call.get("id", "")

        formatted_message = self._format_clarification_message(args)
        request_obj = self._build_request(args)

        logger.info(
            "Clarification requested for thread: %s",
            question[:100],
        )

        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        return Command(
            update={
                "messages": [tool_message],
                "pending_clarification": request_obj,
                "is_finished": True,
            },
            goto=END,
        )

    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """If a pending clarification has been answered, inject the answer."""
        pending = state.get("pending_clarification")
        if pending is None:
            return None

        # Handle both Pydantic model and plain dict
        if hasattr(pending, "answer"):
            answer = pending.answer
            resolved = pending.resolved_at
        else:
            answer = pending.get("answer")
            resolved = pending.get("resolved_at")

        if not answer:
            return None  # Still waiting

        # User has responded — inject answer and clear pending
        answer_msg = HumanMessage(content=answer)
        messages = list(state.get("messages", []))
        messages.append(answer_msg)

        logger.debug("Clarification resolved for thread=%s", state.get("thread_id"))
        return {
            "messages": messages,
            "pending_clarification": None,
        }

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls (sync version)."""
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)
        return self._handle_clarification(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls (async version)."""
        if request.tool_call.get("name") != "ask_clarification":
            return await handler(request)
        return self._handle_clarification(request)

    async def aafter_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Cleanup after agent turn — clear stale pending clarifications."""
        pending = state.get("pending_clarification")
        if pending is not None:
            # If resolved (has answer), clean up stale state
            answer = getattr(pending, "answer", None) or (
                pending.get("answer") if isinstance(pending, dict) else None
            )
            if answer:
                logger.debug("Cleaning resolved clarification for thread=%s", state.get("thread_id"))
                return {"pending_clarification": None}
        return None
