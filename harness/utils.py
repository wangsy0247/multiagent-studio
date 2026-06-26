"""Shared utilities and helpers."""
from __future__ import annotations

import hashlib
import importlib
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool


def resolve_variable(variable_path: str, expected_type: type[Any] | tuple[type[Any], ...] | None = None) -> Any:
    """Resolve a variable from a ``module.path:variable_name`` path.

    Args:
        variable_path: Path like ``harness.tools.search:web_search``.
        expected_type: Optional type(s) to validate against via ``isinstance``.

    Returns:
        The resolved variable.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the variable does not exist in the module.
        ValueError: If the resolved variable fails type validation.
    """
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(
            f"Invalid variable path '{variable_path}', expected 'module.path:variable_name'"
        ) from err

    try:
        module = importlib.import_module(module_path)
    except ImportError as err:
        raise ImportError(f"Could not import module '{module_path}': {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise AttributeError(
            f"Module '{module_path}' does not define attribute '{variable_name}'"
        ) from err

    if expected_type is not None:
        # Support both instance checks (tools, functions) and subclass checks
        # (provider classes, middleware classes).
        if isinstance(variable, type):
            if not issubclass(variable, expected_type):
                raise ValueError(
                    f"Resolved variable '{variable_path}' has type {type(variable)}, "
                    f"expected subclass of {expected_type}"
                )
        elif not isinstance(variable, expected_type):
            raise ValueError(
                f"Resolved variable '{variable_path}' has type {type(variable)}, "
                f"expected {expected_type}"
            )

    return variable


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
    "resolve_variable",
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
