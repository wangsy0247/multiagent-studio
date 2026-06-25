"""Run store — abstract interface and memory implementation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore(ABC):
    """Abstract interface for run metadata persistence."""

    @abstractmethod
    async def put(
        self,
        run_id: str,
        *,
        thread_id: str,
        user_id: str | None = None,
        status: str = "pending",
        model_name: str | None = None,
        first_human_message: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create or update a run record.  Returns the stored dict."""
        ...

    @abstractmethod
    async def get(self, run_id: str) -> dict[str, Any] | None:
        """Retrieve a single run by id."""
        ...

    @abstractmethod
    async def list_by_thread(
        self, thread_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List runs for a thread, most-recent first."""
        ...

    @abstractmethod
    async def update_status(
        self, run_id: str, status: str, error: str | None = None
    ) -> bool:
        """Update run status (idempotent)."""
        ...

    async def update_run_completion(
        self,
        run_id: str,
        status: str,
        *,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        message_count: int = 0,
        last_ai_message: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Update run with final completion data.  Default: no-op."""
        return await self.update_status(run_id, status, error=error)
