"""Shared utilities and helpers."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage


def now_iso() -> str:
    return datetime.now().isoformat()


def hash_text(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def last_ai_message(messages: list[AnyMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def last_tool_messages(messages: list[AnyMessage]) -> list[ToolMessage]:
    result: list[ToolMessage] = []
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            result.append(msg)
        elif isinstance(msg, AIMessage):
            break
    result.reverse()
    return result


def ensure_messages_list(obj: Any) -> list[AnyMessage]:
    if isinstance(obj, list):
        return obj
    if obj is None:
        return []
    return [obj]


__all__ = [
    "now_iso",
    "hash_text",
    "last_ai_message",
    "last_tool_messages",
    "ensure_messages_list",
    "AIMessage",
    "HumanMessage",
    "SystemMessage",
    "ToolMessage",
]
