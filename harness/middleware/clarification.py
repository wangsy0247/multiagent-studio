"""ClarificationMiddleware — human-in-the-loop confirmation and approval."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import ClarificationRequest, HarnessState

logger = logging.getLogger(__name__)


class ClarificationMiddleware(HarnessAgentMiddleware):
    """Intercept ``ask_clarification`` tool calls and create a pending request.

    - ``abefore_agent``: if a pending clarification has been answered, inject
      the answer into the message history.
    - ``aafter_model``: detect ``ask_clarification`` tool calls in the model
      output and set ``pending_clarification`` to pause execution.

    Idempotency: the same question is never asked twice for a thread.
    """

    name = "clarification"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._asked: dict[str, set[str]] = {}

    def cleanup_thread(self, thread_id: str) -> None:
        """Remove per-thread state to prevent memory leaks (#9)."""
        self._asked.pop(thread_id, None)

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

    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Detect ask_clarification tool calls and create a pending request."""
        messages = list(state.get("messages", []))
        thread_id = state.get("thread_id", "default")

        for msg in reversed(messages):
            if not isinstance(msg, AIMessage) or not msg.tool_calls:
                continue
            for tc in msg.tool_calls:
                if tc["name"] != "ask_clarification":
                    continue

                question = tc.get("args", {}).get("question", "请确认")
                q_hash = hashlib.md5(question.encode()).hexdigest()
                asked = self._asked.setdefault(thread_id, set())

                if q_hash in asked:
                    logger.debug("Duplicate clarification skipped: %s", question[:80])
                    continue
                asked.add(q_hash)

                request = ClarificationRequest(
                    id=str(uuid.uuid4()),
                    question=question,
                    context=tc.get("args", {}).get("context", ""),
                    options=tc.get("args", {}).get("options"),
                    required=tc.get("args", {}).get("required", False),
                    created_at=datetime.now(),
                )

                logger.info(
                    "Clarification requested for thread=%s: %s",
                    thread_id, question[:100],
                )
                return {
                    "pending_clarification": request,
                    "is_finished": not request.required,
                }

        return None

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
