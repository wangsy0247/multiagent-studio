"""TodoMiddleware — track and manage plan-mode TODO lists.

Adapted from DeerFlow's TodoMiddleware, extended for HarnessState/TodoItem:

- ``abefore_agent``: clears stale completion-reminder state from the same
  thread's other runs; if plan_mode is set but the todo list is empty the
  middleware marks *context lost* so the agent can recover.
- ``abefore_model``: when the original ``write_todos`` tool call has been
  truncated from the message history (e.g. after summarization) but todos
  still exist in state, injects a reminder so the model stays aware of the
  outstanding todo list.
- ``aafter_model``: if all TODOs are complete/failed it sets
  ``plan_mode_exit``; if the model produces a clean final response (no tool
  calls) while todos are still incomplete, it queues a completion reminder
  and jumps back to the model node to force continued engagement (capped at
  ``_MAX_COMPLETION_REMINDERS`` per run to prevent infinite loops).
- ``awrap_model_call``: injects queued completion reminders into the next
  model request — they are intentionally **not** persisted into the message
  history, so control prompts never leak into user-visible transcripts.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState, TodoItem

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "failed"}
_TOOL_CALL_FINISH_REASONS = {"tool_calls", "function_call"}


# ── Helpers ──────────────────────────────────────────────────────────────

def _todo_status(todo: TodoItem | dict) -> str:
    if isinstance(todo, TodoItem):
        return todo.status
    return todo.get("status", "pending")


def _todo_text(todo: TodoItem | dict) -> str:
    if isinstance(todo, TodoItem):
        return todo.description
    return todo.get("description") or todo.get("content", "")


def _todos_in_messages(messages: list[Any], todo_write_tool_name: str) -> bool:
    """Return True if any AIMessage in *messages* contains a todo-write tool call."""
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == todo_write_tool_name:
                    return True
    return False


def _reminder_in_messages(messages: list[Any]) -> bool:
    """Return True if a todo_reminder HumanMessage is already present in *messages*."""
    for msg in messages:
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == "todo_reminder":
            return True
    return False


def _format_todos(todos: list[TodoItem | dict]) -> str:
    """Format a list of todo items into a human-readable string."""
    return "\n".join(f"- [{_todo_status(t)}] {_todo_text(t)}" for t in todos)


def _format_completion_reminder(todos: list[TodoItem | dict]) -> str:
    """Format a completion reminder for incomplete todo items."""
    incomplete = [t for t in todos if _todo_status(t) not in _TERMINAL_STATUSES]
    incomplete_text = "\n".join(f"- [{_todo_status(t)}] {_todo_text(t)}" for t in incomplete)
    return (
        "<system_reminder>\n"
        "You have incomplete todo items that must be finished before giving your final response:\n\n"
        f"{incomplete_text}\n\n"
        "Please continue working on these tasks. Update the todo list to mark items as completed "
        "as you finish them, and only respond when all items are done.\n"
        "</system_reminder>"
    )


def _has_tool_call_intent_or_error(message: AIMessage) -> bool:
    """Return True when an AIMessage is not a clean final answer.

    Completion reminders should only fire on a plain final response.
    Provider/tool parsing details move across LangChain versions and
    integrations, so keep all tool-intent/error signals behind this helper.
    """
    if message.tool_calls:
        return True

    if getattr(message, "invalid_tool_calls", None):
        return True

    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    if additional_kwargs.get("tool_calls") or additional_kwargs.get("function_call"):
        return True

    response_metadata = getattr(message, "response_metadata", {}) or {}
    return response_metadata.get("finish_reason") in _TOOL_CALL_FINISH_REASONS


class TodoMiddleware(HarnessAgentMiddleware):
    """Plan-mode TODO tracking with context-loss detection and exit prevention."""

    name = "todo"

    # Maximum number of completion reminders before allowing the agent to exit.
    # Prevents infinite loops when the agent cannot make further progress.
    _MAX_COMPLETION_REMINDERS = 2
    # Hard cap for per-run reminder bookkeeping in long-lived middleware instances.
    _MAX_COMPLETION_REMINDER_KEYS = 4096

    def __init__(self, config: dict | None = None, *, todo_write_tool_name: str = "write_todos"):
        super().__init__(config)
        self._todo_write_tool_name = todo_write_tool_name
        self._lock = threading.Lock()
        # (thread_id, run_id) → queued reminder texts / fired reminder count
        self._pending_completion_reminders: dict[tuple[str, str], list[str]] = {}
        self._completion_reminder_counts: dict[tuple[str, str], int] = {}
        self._completion_reminder_touch_order: dict[tuple[str, str], int] = {}
        self._completion_reminder_next_order = 0

    # ── per-run bookkeeping (same convention as LoopDetectionMiddleware) ──

    @staticmethod
    def _get_thread_id(state: HarnessState, runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        thread_id = context.get("thread_id") if context else None
        if not thread_id:
            thread_id = state.get("thread_id")
        return str(thread_id) if thread_id else "default"

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        run_id = context.get("run_id") if context else None
        return str(run_id) if run_id else "default"

    def _pending_key(self, state: HarnessState, runtime: Runtime) -> tuple[str, str]:
        return self._get_thread_id(state, runtime), self._get_run_id(runtime)

    def _touch_completion_reminder_key_locked(self, key: tuple[str, str]) -> None:
        self._completion_reminder_next_order += 1
        self._completion_reminder_touch_order[key] = self._completion_reminder_next_order

    def _completion_reminder_keys_locked(self) -> set[tuple[str, str]]:
        keys = set(self._pending_completion_reminders)
        keys.update(self._completion_reminder_counts)
        keys.update(self._completion_reminder_touch_order)
        return keys

    def _drop_completion_reminder_key_locked(self, key: tuple[str, str]) -> None:
        self._pending_completion_reminders.pop(key, None)
        self._completion_reminder_counts.pop(key, None)
        self._completion_reminder_touch_order.pop(key, None)

    def _prune_completion_reminder_state_locked(self, protected_key: tuple[str, str]) -> None:
        keys = self._completion_reminder_keys_locked()
        overflow = len(keys) - self._MAX_COMPLETION_REMINDER_KEYS
        if overflow <= 0:
            return
        candidates = [key for key in keys if key != protected_key]
        candidates.sort(key=lambda key: self._completion_reminder_touch_order.get(key, 0))
        for key in candidates[:overflow]:
            self._drop_completion_reminder_key_locked(key)

    def _queue_completion_reminder(
        self, state: HarnessState, runtime: Runtime, reminder: str
    ) -> None:
        key = self._pending_key(state, runtime)
        with self._lock:
            self._pending_completion_reminders.setdefault(key, []).append(reminder)
            self._completion_reminder_counts[key] = self._completion_reminder_counts.get(key, 0) + 1
            self._touch_completion_reminder_key_locked(key)
            self._prune_completion_reminder_state_locked(protected_key=key)

    def _completion_reminder_count_for_runtime(
        self, state: HarnessState, runtime: Runtime
    ) -> int:
        key = self._pending_key(state, runtime)
        with self._lock:
            return self._completion_reminder_counts.get(key, 0)

    def _drain_completion_reminders(self, runtime: Runtime) -> list[str]:
        context = getattr(runtime, "context", None)
        thread_id = str(context.get("thread_id")) if context and context.get("thread_id") else "default"
        run_id = str(context.get("run_id")) if context and context.get("run_id") else "default"
        key = (thread_id, run_id)
        with self._lock:
            reminders = self._pending_completion_reminders.pop(key, [])
            if reminders or key in self._completion_reminder_counts:
                self._touch_completion_reminder_key_locked(key)
            return reminders

    def _clear_other_run_completion_reminders(
        self, state: HarnessState, runtime: Runtime
    ) -> None:
        thread_id, current_run_id = self._pending_key(state, runtime)
        with self._lock:
            for key in self._completion_reminder_keys_locked():
                if key[0] == thread_id and key[1] != current_run_id:
                    self._drop_completion_reminder_key_locked(key)

    def _clear_current_run_completion_reminders(
        self, state: HarnessState, runtime: Runtime
    ) -> None:
        key = self._pending_key(state, runtime)
        with self._lock:
            self._drop_completion_reminder_key_locked(key)

    # ── Hooks ────────────────────────────────────────────────────────────

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        self._clear_other_run_completion_reminders(state, runtime)
        if state.get("plan_mode") and not state.get("todos"):
            logger.warning("Plan-mode context lost for thread=%s", state.get("thread_id"))
            return {"context_lost": True}
        return None

    @override
    async def abefore_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Inject a todo-list reminder when the todo-write call has left the context window."""
        todos = state.get("todos") or []
        if not todos:
            return None

        messages = state.get("messages") or []
        if _todos_in_messages(messages, self._todo_write_tool_name):
            # Todo write is still visible in context — nothing to do.
            return None

        if _reminder_in_messages(messages):
            # A reminder was already injected and hasn't been truncated yet.
            return None

        formatted = _format_todos(todos)
        reminder = HumanMessage(
            name="todo_reminder",
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "Your todo list from earlier is no longer visible in the current context window, "
                "but it is still active. Here is the current state:\n\n"
                f"{formatted}\n\n"
                "Continue tracking and updating this todo list as you work. "
                "Update the todo list whenever the status of any item changes.\n"
                "</system_reminder>"
            ),
        )
        return {"messages": [reminder]}

    @override
    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        todos = state.get("todos") or []
        if not todos:
            return None

        # Existing behaviour: everything resolved → signal plan-mode exit.
        if all(_todo_status(t) in _TERMINAL_STATUSES for t in todos):
            logger.debug("All TODOs resolved — plan_mode_exit set")
            return {"plan_mode_exit": True}

        # Only intervene on a clean exit (plain final response). Tool-call
        # intent or tool-call parse errors belong to the tool path.
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or _has_tool_call_intent_or_error(last_ai):
            return None

        # Enforce a reminder cap to prevent infinite re-engagement loops.
        if (
            self._completion_reminder_count_for_runtime(state, runtime)
            >= self._MAX_COMPLETION_REMINDERS
        ):
            return None

        # Queue a reminder for the next model request and jump back. The
        # reminder is delivered via awrap_model_call instead of being
        # persisted into graph state, so it never leaks into transcripts.
        self._queue_completion_reminder(state, runtime, _format_completion_reminder(todos))
        return {"jump_to": "model"}

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        reminders = self._drain_completion_reminders(request.runtime)
        if not reminders:
            return await handler(request)
        new_messages = [
            *request.messages,
            HumanMessage(
                content="\n\n".join(dict.fromkeys(reminders)),
                name="todo_completion_reminder",
                additional_kwargs={"hide_from_ui": True},
            ),
        ]
        return await handler(request.override(messages=new_messages))

    @override
    async def aafter_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        self._clear_current_run_completion_reminders(state, runtime)
        return None
