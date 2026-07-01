"""Harness memory package — DeerFlow-aligned.

Memory is a global singleton system:
- ``FileMemoryStorage`` — single JSON file per user (file backend)
- ``mem0_client`` — mem0 + Chroma vector store (mem0 backend)
- ``MemoryUpdateQueue`` — debounced update queue (threading.Timer)
- ``MemoryUpdater`` — LLM-driven memory extraction and persistence
- ``DynamicContextMiddleware`` — reads + injects memory at ``abefore_agent``
- ``MemoryMiddleware`` — queues updates at ``aafter_agent``
"""

from harness.memory.prompt import (
    FACT_EXTRACTION_PROMPT,
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from harness.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
    reset_memory_queue,
)
from harness.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
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
    # Updater
    "MemoryUpdater",
    "clear_memory_data",
    "get_memory_data",
]
