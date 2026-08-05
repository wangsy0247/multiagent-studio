"""ClarificationMiddleware — human-in-the-loop confirmation and approval.

Matches the harness design: intercept ``ask_clarification`` tool calls
before they execute and return a ``Command`` that jumps to ``END`` with the
formatted question stored as a ``ToolMessage``. This cleanly stops the ReAct
loop and lets the frontend present the clarification request.

Clarification metadata is attached to the ``ToolMessage`` via
``additional_kwargs["clarification"]`` so it can be recovered later without
relying on a custom LangGraph state key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, override
from uuid import uuid4

from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)


def extract_clarification_from_tool_message(msg: Any) -> dict[str, Any] | None:
    """Extract structured clarification metadata from an ask_clarification ToolMessage.

    The metadata is stored in ``additional_kwargs["clarification"]`` when the
    middleware creates the message. If it is missing, a best-effort fallback
    returns the message content as the question.
    """
    msg_type = getattr(msg, "type", None)
    if msg_type != "tool":
        return None
    if getattr(msg, "name", None) != "ask_clarification":
        return None

    additional = getattr(msg, "additional_kwargs", None) or {}
    clarification = additional.get("clarification")
    if clarification and isinstance(clarification, dict):
        return clarification

    content = getattr(msg, "content", "")
    return {"question": content, "context": "", "options": [], "required": False}


def get_pending_clarification(messages: list[Any]) -> dict[str, Any] | None:
    """Return the pending clarification if the conversation is waiting for one.

    Walks backwards through ``messages``. If the last non-human message is an
    ``ask_clarification`` ``ToolMessage`` and there is no ``HumanMessage`` after
    it, the clarification is still pending. Otherwise returns ``None``.
    """
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", None)
        if msg_type == "human":
            return None
        if msg_type == "tool" and getattr(msg, "name", None) == "ask_clarification":
            return extract_clarification_from_tool_message(msg)
    return None


class ClarificationMiddleware(HarnessAgentMiddleware):
    """Intercept ``ask_clarification`` tool calls and interrupt execution.

    When the model calls ``ask_clarification``, this middleware intercepts
    the call before it executes and returns ``Command(goto=END)`` with a
    ``ToolMessage`` containing the formatted question. The frontend renders
    the clarification card, and the worker resumes execution when the user
    answers.

    This follows the harness's message-based state management: no custom
    ``pending_clarification`` state key is used. The pending clarification is
    inferred directly from the message history.
    """

    name = "clarification"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stable_message_id(tool_call_id: str, formatted_message: str) -> str:
        if tool_call_id:
            return f"clarification:{tool_call_id}"
        digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
        return f"clarification:{digest}"

    def _format_clarification_message(self, args: dict) -> str:
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
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

    def _build_clarification_metadata(self, args: dict) -> dict[str, Any]:
        """Build the structured clarification payload stored on the ToolMessage."""
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

        return {
            "id": str(uuid4()),
            "question": args.get("question", "请确认"),
            "context": args.get("context", ""),
            "options": options,
            "required": args.get("required", False),
            "clarification_type": args.get("clarification_type", "missing_info"),
        }

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        tool_call = request.tool_call
        args = tool_call.get("args", {})
        tool_call_id = tool_call.get("id", "")

        formatted_message = self._format_clarification_message(args)
        metadata = self._build_clarification_metadata(args)

        logger.info("Clarification requested: %s", args.get("question", "")[:100])

        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
            additional_kwargs={"clarification": metadata},
        )

        return Command(
            update={"messages": [tool_message]},
            goto=END,
        )

    @staticmethod
    def _is_unattended(request: ToolCallRequest) -> bool:
        """无人值守执行（定时任务）判定：state.metadata.unattended 由 HarnessService.execute 注入"""
        state = getattr(request, "state", None) or {}
        metadata = state.get("metadata") if isinstance(state, dict) else None
        return bool((metadata or {}).get("unattended"))

    def _handle_unattended(self, request: ToolCallRequest) -> ToolMessage:
        """无人值守执行中的澄清请求：不暂停，指示模型自行决策（无人可回答问题）"""
        tool_call = request.tool_call
        logger.info(
            "Unattended run: ask_clarification suppressed (%s)",
            (tool_call.get("args", {}) or {}).get("question", "")[:80],
        )
        return ToolMessage(
            id=self._stable_message_id(tool_call.get("id", ""), "unattended"),
            content=(
                "当前为无人值守执行（定时任务），无法向用户提问。"
                "请根据已有信息自行做出合理决策并继续完成任务，若无法进行判断则进行总结返回，待用户查看时决定。"
                "不要再次调用 ask_clarification。"
            ),
            tool_call_id=tool_call.get("id", ""),
            name="ask_clarification",
        )

    # ------------------------------------------------------------------
    # hooks — wrap_tool_call interrupts
    # ------------------------------------------------------------------

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)
        if self._is_unattended(request):
            return self._handle_unattended(request)
        return self._handle_clarification(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            return await handler(request)
        if self._is_unattended(request):
            return self._handle_unattended(request)
        return self._handle_clarification(request)
