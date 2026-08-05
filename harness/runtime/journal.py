"""RunJournal — LangChain callback that captures LLM/tool events.

harness-aligned: writes lifecycle, message, and trace events to a pluggable
``RunEventStore``. Also supports sub-agent token attribution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from harness.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)


class RunJournal(BaseCallbackHandler):
    """LangChain ``BaseCallbackHandler`` that buffers and flushes events.

    Parameters
    ----------
    thread_id : str
    run_id : str
    user_id : str | None
    event_store : RunEventStore
    track_token_usage : bool
    flush_threshold : int
    """

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        user_id: str | None,
        event_store: RunEventStore,
        *,
        track_token_usage: bool = True,
        flush_threshold: int = 20,
    ) -> None:
        super().__init__()
        self._thread_id = thread_id
        self._run_id = run_id
        self._user_id = user_id
        self._event_store = event_store
        self._track_token_usage = track_token_usage
        self._flush_threshold = flush_threshold

        self._buffer: list[dict[str, Any]] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_tokens = 0
        self._llm_call_count = 0
        self._first_human_message: str | None = None
        self._last_ai_message: str | None = None
        self._message_count = 0

        # ── Sub-agent token attribution ──────────────────────────────────
        # Maps subagent_name → cumulative token usage
        self._subagent_tokens: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # LangChain callbacks
    # ------------------------------------------------------------------

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        if self._first_human_message is not None:
            return
        try:
            for batch in messages:
                for msg in batch:
                    if getattr(msg, "type", None) == "human":
                        content = getattr(msg, "content", "")
                        self._first_human_message = (
                            str(content)[:2000] if content else None
                        )
                        return
        except Exception:
            pass

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._llm_call_count += 1

        content = ""
        try:
            generations = getattr(response, "generations", [[]])
            if generations and generations[0]:
                msg = getattr(generations[0][0], "message", None)
                if msg:
                    content = getattr(msg, "content", "")
        except Exception:
            pass

        if content:
            self._last_ai_message = str(content)[:2000]
            self._message_count += 1

        if self._track_token_usage:
            try:
                usage = (
                    getattr(response, "usage_metadata", None)
                    or getattr(
                        getattr(response, "response_metadata", {}),
                        "token_usage",
                        {},
                    )
                )
                if usage:
                    self._total_input_tokens += usage.get("input_tokens", 0)
                    self._total_output_tokens += usage.get("output_tokens", 0)
                    self._total_tokens += usage.get("total_tokens", 0)
            except Exception:
                pass

        self._add_event(
            event_type="llm.ai.response",
            category="message",
            content=content,
        )

    async def on_tool_end(
        self,
        output: str,
        *,
        run_id: str,
        parent_run_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        content = ""
        if hasattr(output, "content"):
            content = str(output.content)
        elif isinstance(output, str):
            content = output
        else:
            try:
                content = json.dumps(output, ensure_ascii=False)
            except Exception:
                content = str(output)

        self._add_event(
            event_type="llm.tool.result",
            category="trace",
            content=str(content)[:2000],
        )

    # ------------------------------------------------------------------
    # Sub-agent token attribution (harness-aligned)
    # ------------------------------------------------------------------

    def record_subagent_tokens(
        self,
        subagent_name: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        """Record token usage from a sub-agent execution.

        Accumulates per-subagent-name so the lead agent's completion data
        includes a breakdown of which sub-agent consumed how many tokens.
        """
        if subagent_name not in self._subagent_tokens:
            self._subagent_tokens[subagent_name] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        st = self._subagent_tokens[subagent_name]
        st["input_tokens"] += input_tokens
        st["output_tokens"] += output_tokens
        st["total_tokens"] += total_tokens

        # Also add to lead agent totals
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._total_tokens += total_tokens

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def set_first_human_message(self, text: str) -> None:
        if self._first_human_message is None:
            self._first_human_message = text[:2000]

    def get_completion_data(self) -> dict[str, Any]:
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_tokens,
            "llm_call_count": self._llm_call_count,
            "message_count": self._message_count,
            "first_human_message": self._first_human_message,
            "last_ai_message": self._last_ai_message,
            "subagent_tokens": self._subagent_tokens,
        }

    async def flush(self) -> None:
        if self._buffer:
            batch = self._buffer
            self._buffer = []
            try:
                await self._event_store.put_batch(batch)
            except Exception:
                logger.exception("Failed to flush journal buffer")

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _add_event(self, **kwargs: Any) -> None:
        evt = {
            "thread_id": self._thread_id,
            "run_id": self._run_id,
            "user_id": self._user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self._buffer.append(evt)
        if len(self._buffer) >= self._flush_threshold:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._buffer:
            batch = self._buffer
            self._buffer = []
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._event_store.put_batch(batch))
            except RuntimeError:
                pass
