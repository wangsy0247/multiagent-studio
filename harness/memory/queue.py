"""Memory update queue with asyncio debounce — adapted from DeerFlow.

Unlike the previous ``threading.Timer`` implementation, this version uses
``asyncio.create_task`` + ``asyncio.sleep`` for debouncing, which:

- Integrates naturally with the FastAPI event loop
- Survives Harness restarts by flushing at shutdown
- Uses ``aupdate_memory()`` (async LLM) instead of ``update_memory()`` (sync)
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harness.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context for a conversation to be processed for memory update."""

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False


class MemoryUpdateQueue:
    """Queue for memory updates with asyncio debounce mechanism.

    This queue collects conversation contexts and processes them after
    a configurable debounce period. Multiple conversations received within
    the debounce window are batched together.

    Async design:
    - ``add()`` / ``add_nowait()`` schedule an asyncio Task
    - ``_debounced_process()`` waits (asyncio.sleep) then processes
    - ``flush()`` cancels pending task and processes immediately (at shutdown)
    - Thread-safe queue access via ``threading.Lock``
    """

    def __init__(self):
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._processing = False

    @staticmethod
    def _queue_key(
        thread_id: str,
        user_id: str | None,
        agent_name: str | None,
    ) -> tuple[str, str | None, str | None]:
        return (thread_id, user_id, agent_name)

    # ── Public API ───────────────────────────────────────────────────────

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation to the update queue (debounced)."""
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id, messages=messages,
                agent_name=agent_name, user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._ensure_task()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation and process immediately (zero debounce)."""
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id, messages=messages,
                agent_name=agent_name, user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._ensure_task(delay=0)

        logger.info("Memory update queued for immediate processing on thread %s", thread_id)

    async def flush(self) -> None:
        """Cancel pending task and process queue immediately (for shutdown)."""
        with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                self._task = None
        await self._process_queue()

    def clear(self) -> None:
        """Clear the queue without processing."""
        with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                self._task = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        with self._lock:
            return self._processing

    # ── Internal ─────────────────────────────────────────────────────────

    def _enqueue_locked(self, *, thread_id, messages, agent_name, user_id,
                        correction_detected, reinforcement_detected) -> None:
        queue_key = self._queue_key(thread_id, user_id, agent_name)
        existing_context = next(
            (c for c in self._queue
             if self._queue_key(c.thread_id, c.user_id, c.agent_name) == queue_key),
            None,
        )
        merged_correction = correction_detected or (
            existing_context.correction_detected if existing_context else False)
        merged_reinforcement = reinforcement_detected or (
            existing_context.reinforcement_detected if existing_context else False)
        context = ConversationContext(
            thread_id=thread_id, messages=messages,
            agent_name=agent_name, user_id=user_id,
            correction_detected=merged_correction,
            reinforcement_detected=merged_reinforcement,
        )
        self._queue = [c for c in self._queue
                       if self._queue_key(c.thread_id, c.user_id, c.agent_name) != queue_key]
        self._queue.append(context)

    def _ensure_task(self, *, delay: float | None = None) -> None:
        """Create asyncio task for debounced processing if not already pending."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop (should not happen in FastAPI context)
            logger.warning("MemoryUpdateQueue: no running event loop, skipping task creation")
            return

        # Cancel existing pending task
        if self._task and not self._task.done():
            self._task.cancel()

        config = get_memory_config()
        debounce = delay if delay is not None else config.debounce_seconds
        self._task = loop.create_task(self._debounced_process(debounce))
        logger.debug("Memory update task scheduled (delay=%ss)", debounce)

    async def _debounced_process(self, delay: float) -> None:
        """Wait for debounce period then process the queue."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._process_queue()

    async def _process_queue(self) -> None:
        from harness.memory.updater import MemoryUpdater

        with self._lock:
            if self._processing:
                # Another task is already processing; reschedule
                self._ensure_task()
                return
            if not self._queue:
                return
            self._processing = True
            contexts_to_process = self._queue.copy()
            self._queue.clear()
            self._task = None

        logger.info("Processing %d queued memory updates (async)", len(contexts_to_process))

        try:
            updater = MemoryUpdater()
            for context in contexts_to_process:
                try:
                    logger.info("Updating memory for thread %s (async)", context.thread_id)
                    success = await updater.aupdate_memory(
                        messages=context.messages,
                        thread_id=context.thread_id,
                        agent_name=context.agent_name,
                        correction_detected=context.correction_detected,
                        reinforcement_detected=context.reinforcement_detected,
                        user_id=context.user_id,
                    )
                    if success:
                        logger.info("Memory updated successfully for thread %s", context.thread_id)
                    else:
                        logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                except Exception as e:
                    logger.error("Error updating memory for thread %s: %s", context.thread_id, e)
                if len(contexts_to_process) > 1:
                    await asyncio.sleep(0.5)
        finally:
            with self._lock:
                self._processing = False


# ── Global singleton ──────────────────────────────────────────────────────
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
