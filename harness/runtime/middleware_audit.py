"""Helper for middlewares to record audit events into RunJournal."""

from __future__ import annotations
import logging
from typing import Any
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


def audit(runtime: Runtime, name: str, hook: str, action: str, changes: dict[str, Any] | None = None) -> None:
    """Record a middleware audit event into the current run's RunJournal.

    Usage from within any middleware hook::

        from harness.runtime.middleware_audit import audit
        audit(runtime, self.name, "aafter_model", "loop_detected",
              changes={"count": 3, "hash": "abc123"})

    This is a no-op if no RunJournal is registered for the current run.
    """
    run_id = runtime.context.get("run_id", "") if runtime.context else ""
    if not run_id:
        logger.debug("audit skipped: no run_id in runtime.context (keys=%s)",
                      list(runtime.context.keys()) if runtime.context else [])
        return
    from harness.runtime.journal import get_run_journal
    journal = get_run_journal(str(run_id))
    if journal is None:
        logger.debug("audit skipped: no journal for run_id=%s", run_id)
        return
    journal.record_middleware(
        tag=f"middleware:{name}",
        name=name,
        hook=hook,
        action=action,
        changes=changes or {},
    )
