"""Hook fired before summarization removes messages from state — adapted from DeerFlow."""

from __future__ import annotations

from harness.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from harness.memory.queue import get_memory_queue
from harness.config.memory_config import get_memory_config


def memory_flush_hook(event) -> None:
    """Flush messages about to be summarized into the memory queue.

    Uses ``add_nowait()`` for immediate processing, since the messages are
    about to be removed from state by summarization.
    """
    if not get_memory_config().enabled or not event.thread_id:
        return

    filtered_messages = filter_messages_for_memory(list(event.messages_to_summarize))
    user_messages = [m for m in filtered_messages if getattr(m, "type", None) == "human"]
    assistant_messages = [m for m in filtered_messages if getattr(m, "type", None) == "ai"]
    if not user_messages or not assistant_messages:
        return

    correction_detected = detect_correction(filtered_messages)
    reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)

    # Resolve user_id from runtime or event
    user_id = None
    if hasattr(event, 'runtime') and event.runtime:
        ctx = getattr(event.runtime, 'context', None)
        if ctx:
            user_id = ctx.get("user_id")

    queue = get_memory_queue()
    queue.add_nowait(
        thread_id=event.thread_id,
        messages=filtered_messages,
        agent_name=getattr(event, 'agent_name', None),
        user_id=user_id,
        correction_detected=correction_detected,
        reinforcement_detected=reinforcement_detected,
    )
