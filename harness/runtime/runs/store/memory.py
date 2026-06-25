"""In-memory RunStore implementation."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from harness.runtime.runs.store.base import RunStore

logger = logging.getLogger(__name__)


class MemoryRunStore(RunStore):
    """Dict-backed run store — no persistence across restarts."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def put(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        now = self._now()
        existing = self._runs.get(run_id)
        if existing:
            existing.update(kwargs)
            existing["updated_at"] = now
        else:
            self._runs[run_id] = {
                "run_id": run_id,
                "created_at": now,
                "updated_at": now,
                **kwargs,
            }
        return dict(self._runs[run_id])

    async def get(self, run_id: str) -> dict[str, Any] | None:
        rec = self._runs.get(run_id)
        return dict(rec) if rec else None

    async def list_by_thread(
        self, thread_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        items = [
            dict(r)
            for r in self._runs.values()
            if r.get("thread_id") == thread_id
        ]
        items.sort(
            key=lambda r: r.get("created_at", ""), reverse=True
        )
        return items[:limit]

    async def update_status(
        self, run_id: str, status: str, error: str | None = None
    ) -> bool:
        rec = self._runs.get(run_id)
        if rec is None:
            return False
        rec["status"] = status
        rec["updated_at"] = self._now()
        if error:
            rec["error"] = error
        return True

    async def update_run_completion(
        self, run_id: str, status: str, **kwargs: Any
    ) -> bool:
        rec = self._runs.get(run_id)
        if rec is None:
            return False
        rec["status"] = status
        rec["updated_at"] = self._now()
        for key in (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "llm_call_count",
            "message_count",
            "last_ai_message",
            "error",
        ):
            if key in kwargs and kwargs[key]:
                rec[key] = kwargs[key]
        return True
