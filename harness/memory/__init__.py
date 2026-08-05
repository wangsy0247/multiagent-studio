"""Harness memory package — harness-aligned.

Memory is a global singleton system:
- ``FileMemoryStorage`` — single JSON file per user (file backend)
- ``MemoryUpdateQueue`` — debounced update queue (threading.Timer)
- ``MemoryUpdater`` — LLM-driven memory extraction and persistence
- ``DynamicContextMiddleware`` — reads + injects memory at ``abefore_agent``
- ``MemoryMiddleware`` — queues updates at ``aafter_agent``
"""

from harness.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from harness.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
)
from harness.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
)
from harness.memory.task_memory import (
    TaskMemory,
    TaskMemoryStore,
)
from harness.memory.team_memory import (
    TeamMemory,
    TeamMemoryStore,
)
from harness.memory.updater import (
    MemoryUpdater,
    clear_memory_data,
    get_memory_data,
)

__all__ = [
    # Prompt
    "MEMORY_UPDATE_PROMPT",
    "format_memory_for_injection",
    "format_conversation_for_update",
    # Queue
    "ConversationContext",
    "MemoryUpdateQueue",
    "get_memory_queue",
    # Storage
    "MemoryStorage",
    "FileMemoryStorage",
    "get_memory_storage",
    # Task memory
    "TaskMemory",
    "TaskMemoryStore",
    # Team memory
    "TeamMemory",
    "TeamMemoryStore",
    # Updater
    "MemoryUpdater",
    "clear_memory_data",
    "get_memory_data",
]
