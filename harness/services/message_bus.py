"""SSE message bus for streaming events to the frontend."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class SSEEventType:
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    CLARIFICATION = "clarification"
    TODO_UPDATE = "todo_update"
    TITLE_UPDATE = "title_update"
    MEMORY_UPDATE = "memory_update"
    TOKEN_USAGE = "token_usage"
    EVALUATION = "evaluation"
    ERROR = "error"
    FINISHED = "finished"


class MessageBus:
    """Manage SSE connections and event distribution."""

    def __init__(self):
        self._connections: dict[str, asyncio.Queue] = {}

    async def connect(self, thread_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._connections[thread_id] = queue
        return queue

    async def disconnect(self, thread_id: str) -> None:
        self._connections.pop(thread_id, None)

    async def emit(self, thread_id: str, event: dict[str, Any]) -> None:
        if thread_id in self._connections:
            await self._connections[thread_id].put(event)

    def format_sse(self, event: dict[str, Any]) -> str:
        return f"data: {json.dumps(event, default=str)}\n\n"

    async def broadcast(self, event: dict[str, Any]) -> None:
        for thread_id in list(self._connections):
            await self.emit(thread_id, event)
