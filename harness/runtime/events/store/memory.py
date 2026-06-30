"""In-memory RunEventStore implementation."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from harness.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)


class MemoryRunEventStore(RunEventStore):
    """Dict-backed event store — no persistence across restarts."""

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._seq_counters: dict[str, int] = {}

    def _next_seq(self, thread_id: str) -> int:
        seq = self._seq_counters.get(thread_id, 0) + 1
        self._seq_counters[thread_id] = seq
        return seq

    async def put(self, thread_id: str, run_id: str, **kwargs: Any) -> dict[str, Any]:
        record = {
            "thread_id": thread_id,
            "run_id": run_id,
            "seq": self._next_seq(thread_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self._events.setdefault(thread_id, []).append(record)
        return dict(record)

    async def put_batch(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for evt in events:
            rec = await self.put(
                thread_id=evt.get("thread_id", ""),
                run_id=evt.get("run_id", ""),
                event_type=evt.get("event_type", "unknown"),
                category=evt.get("category", "trace"),
                content=evt.get("content", ""),
                metadata=evt.get("metadata", {}),
                user_id=evt.get("user_id"),
                timestamp=evt.get("timestamp"),
            )
            results.append(rec)
        return results

    async def list_messages(
        self, thread_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        thread_events = self._events.get(thread_id, [])
        messages = [e for e in thread_events if e.get("category") == "message"]
        messages.sort(key=lambda e: e.get("seq", 0))
        return messages[-limit:]

    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        event_types: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        thread_events = self._events.get(thread_id, [])
        filtered = [
            e
            for e in thread_events
            if e.get("run_id") == run_id
            and (event_types is None or e.get("event_type") in event_types)
        ]
        filtered.sort(key=lambda e: e.get("seq", 0))
        return filtered[-limit:]
