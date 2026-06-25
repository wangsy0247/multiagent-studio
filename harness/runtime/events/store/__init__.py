"""Event-store factory."""
from __future__ import annotations

from harness.runtime.events.store.base import RunEventStore
from harness.runtime.events.store.memory import MemoryRunEventStore


def make_event_store() -> RunEventStore:
    """Return the appropriate RunEventStore for the current configuration.

    Currently always returns ``MemoryRunEventStore`` (DB-backed store is a
    future iteration).
    """
    return MemoryRunEventStore()
