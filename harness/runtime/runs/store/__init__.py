"""Run-store factory."""
from __future__ import annotations

from harness.runtime.runs.store.base import RunStore
from harness.runtime.runs.store.memory import MemoryRunStore


def make_run_store() -> RunStore:
    """Return the appropriate RunStore for the current configuration.

    Currently always returns ``MemoryRunStore`` (DB-backed store is a
    future iteration).
    """
    return MemoryRunStore()
