"""SafetyFinishReasonMiddleware — suppress tool calls on provider safety termination.

Matches DeerFlow's design: runs at ``aafter_model``, detects when a provider
safety-terminated the response (e.g. ``finish_reason=content_filter``) but
still emitted partial ``tool_calls``. Strips those tool calls and appends
a user-facing explanation.
"""

from __future__ import annotations

import logging
from typing import override

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

# ── Detector helpers ──────────────────────────────────────────────────────

_USER_FACING_MESSAGE = (
    "The model provider stopped this response with a safety-related signal. "
    "Any tool calls in this response have been suppressed."
)


def _detect_safety_termination(msg: AIMessage) -> str | None:
    """Return a detector label if this AIMessage was safety-terminated, or None."""
    finish_reason = msg.response_metadata.get("finish_reason", "") if msg.response_metadata else ""
    stop_reason = msg.response_metadata.get("stop_reason", "") if msg.response_metadata else ""

    # OpenAI: finish_reason=content_filter
    if finish_reason == "content_filter":
        return "openai_content_filter"

    # Anthropic: stop_reason=refusal
    if stop_reason == "refusal":
        return "anthropic_refusal"

    # Gemini: finish_reason=SAFETY
    if finish_reason in ("SAFETY", "RECITATION") if isinstance(finish_reason, str) else False:
        return f"gemini_{finish_reason.lower()}"

    return None


# ── Middleware ─────────────────────────────────────────────────────────────

class SafetyFinishReasonMiddleware(HarnessAgentMiddleware):
    """Detect provider safety terminations and suppress tool calls.

    Runs in ``aafter_model``. When a safety termination is detected and the
    response still carries ``tool_calls``, the tool calls are stripped and
    a user-facing explanation is appended.
    """

    name = "safety_finish_reason"

    def __init__(self, config: dict | None = None, *, enabled: bool = True):
        super().__init__(config)
        self._enabled = enabled

    @override
    async def aafter_model(self, state: HarnessState, runtime: Runtime) -> dict | None:
        if not self._enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        detector = _detect_safety_termination(last_msg)
        if detector is None:
            return None

        logger.warning(
            "Safety termination detected: detector=%s thread=%s",
            detector, state.get("thread_id"),
        )

        # Strip tool_calls from the AIMessage
        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "function_call"):
            additional_kwargs.pop(key, None)
        additional_kwargs["safety_termination"] = {
            "detector": detector,
            "finish_reason": last_msg.response_metadata.get("finish_reason") if last_msg.response_metadata else None,
        }

        new_content = (last_msg.content or "") + f"\n\n{_USER_FACING_MESSAGE}"
        stripped_msg = last_msg.model_copy(update={
            "tool_calls": [],
            "content": new_content,
            "additional_kwargs": additional_kwargs,
        })

        return {"messages": [stripped_msg]}
