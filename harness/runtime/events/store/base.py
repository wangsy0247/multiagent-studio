"""Run-event store — abstract interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RunEventStore(ABC):
    """Abstract interface for run-event persistence."""

    @abstractmethod
    async def put(
        self,
        thread_id: str,
        run_id: str,
        *,
        event_type: str,
        category: str = "trace",
        content: str = "",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a single event.  Returns the stored dict (including seq)."""
        ...

    @abstractmethod
    async def put_batch(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Write multiple events in one call."""
        ...

    @abstractmethod
    async def list_messages(
        self, thread_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List message-category events for a thread, ordered by seq."""
        ...

    @abstractmethod
    async def list_events(
        self,
        thread_id: str,
        run_id: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List events for a specific run (or all if run_id is None), ordered by seq."""
        ...
