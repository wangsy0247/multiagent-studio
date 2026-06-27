"""MemoryMiddleware — queue conversation for memory update after agent execution.

Adapted from DeerFlow: only ``aafter_agent`` (no ``abefore_agent``).
Memory reading/injection is handled by DynamicContextMiddleware.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, override

from langgraph.config import get_config
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.memory.message_processing import (
    detect_correction,
    detect_reinforcement,
    filter_messages_for_memory,
)
from harness.memory.queue import get_memory_queue
from harness.config.memory_config import get_memory_config
from harness.models import HarnessState

if TYPE_CHECKING:
    from harness.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryMiddleware(HarnessAgentMiddleware):
    """Middleware that queues conversation for memory update after agent execution.

    This middleware:
    1. After each agent execution, queues the conversation for memory update
    2. Only includes user inputs and final assistant responses (ignores tool calls)
    3. The queue uses debouncing to batch multiple updates together
    4. Memory is updated asynchronously via LLM summarization
    """

    name = "memory"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None,
                 memory_config: "MemoryConfig | None" = None):
        super().__init__(config)
        self._agent_name = agent_name
        self._memory_config = memory_config

    @override
    async def aafter_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Queue conversation for memory update after agent completes."""
        mem_cfg = self._memory_config or get_memory_config()
        if not mem_cfg.enabled:
            return None

        # Get thread_id from runtime context or LangGraph config
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            config_data = get_config()
            thread_id = config_data.get("configurable", {}).get("thread_id")
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        # Filter to only keep user inputs and final assistant responses
        filtered = filter_messages_for_memory(messages)

        user_messages = [m for m in filtered if getattr(m, "type", None) == "human"]
        assistant_messages = [m for m in filtered if getattr(m, "type", None) == "ai"]
        if not user_messages or not assistant_messages:
            return None

        correction_detected = detect_correction(filtered)
        reinforcement_detected = not correction_detected and detect_reinforcement(filtered)

        user_id = state.get("user_id", "")
        queue = get_memory_queue()
        queue.add(
            thread_id=thread_id,
            messages=filtered,
            agent_name=self._agent_name,
            user_id=user_id,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
        )

        return None
