"""Built-in ``write_todos`` tool — plan 模式的 TODO 列表写入入口.

对齐 Claude Code 的 TodoWrite / langchain ``TodoListMiddleware`` 的
``write_todos``，但写入 harness 自己的 ``HarnessState.todos``
(``TodoItem{id,description,status}`` + ``merge_todos`` reducer):

- 带 ``id`` 的入参项 → 按 id 更新既有项 (reducer 归并语义);
- 不带 ``id`` 的入参项 → 先按 description 匹配现有项 (幂等全量重写,
  模型通常每轮发完整列表), 匹配不到才生成新 id;
- 删除语义不支持 — 用 ``failed`` 状态标记取消的项。

SSE 侧由 ``harness/main.py`` 的 ``on_tool_end`` 分支把整表 todos
以 ``todo_update`` 事件推给前端。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command
from pydantic import BaseModel, Field

from harness.models import TodoItem

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "failed"}

WRITE_TODOS_DESCRIPTION = """Use this tool to create and manage a structured task list for your current work session. This helps you track progress and organize complex tasks.

## When to Use

1. Complex multi-step tasks — 3 or more distinct steps or actions
2. Non-trivial tasks requiring careful planning or multiple operations
3. The user explicitly requests a todo list or provides multiple tasks at once
4. Plan mode is active — you MUST write the plan as todos BEFORE starting execution

## How to Use

1. Send the FULL list on every call — each call replaces the status of the items it mentions.
2. When you start working on a task, mark it in_progress BEFORE beginning; mark it completed right after finishing.
3. To update an existing item, include its `id` (returned by previous calls) or repeat its exact `description`.
4. You may add newly discovered follow-up tasks at any time. Do not rewrite previously completed items.
5. Never call this tool multiple times in parallel.

## When NOT to Use

- A single straightforward task, or anything completable in fewer than 3 trivial steps
- Purely conversational or informational requests

Remember: `write_todos` tracks your work; it does not deliver the answer. Whatever the user asked for must appear as text content in a message AFTER your final `write_todos` call."""


class WriteTodoEntry(BaseModel):
    """Single incoming todo entry from the model."""

    id: str | None = Field(default=None, description="Existing todo id to update; omit to create")
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"


def _existing_todos(state: Any) -> list[TodoItem]:
    """Best-effort read of current todos from graph state."""
    raw = (state or {}).get("todos") or []
    items: list[TodoItem] = []
    for t in raw:
        if isinstance(t, TodoItem):
            items.append(t)
        elif isinstance(t, dict):
            try:
                items.append(TodoItem(**t))
            except Exception:
                continue
    return items


def write_todos_tool() -> BaseTool:
    """Create the ``write_todos`` tool used by the Lead Agent."""

    @tool("write_todos", description=WRITE_TODOS_DESCRIPTION)
    async def write_todos(
        todos: list[WriteTodoEntry],
        runtime: ToolRuntime,
    ) -> Command:
        # runtime 由框架注入 (_DirectlyInjectedToolArg), 不出现在 tool schema 中
        existing = _existing_todos(runtime.state)
        by_id = {t.id: t for t in existing}
        by_desc = {t.description: t for t in existing}

        updated: list[TodoItem] = []
        for entry in todos:
            if not entry.description.strip():
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                "Error: todo description must not be empty",
                                tool_call_id=runtime.tool_call_id,
                            )
                        ]
                    }
                )
            # 匹配优先级: 显式 id > description 幂等匹配 > 新建
            base = None
            if entry.id and entry.id in by_id:
                base = by_id[entry.id]
            elif entry.description in by_desc:
                base = by_desc[entry.description]

            if base is not None:
                item = base.model_copy(
                    update={"description": entry.description, "status": entry.status}
                )
            else:
                item = TodoItem(description=entry.description, status=entry.status)

            if entry.status in _TERMINAL_STATUSES and item.completed_at is None:
                item.completed_at = datetime.now()
            updated.append(item)

        # merge_todos reducer 按 id 归并: 传整表即可, 旧项状态被覆盖
        return Command(
            update={
                "todos": updated,
                "messages": [
                    ToolMessage(
                        f"Updated todo list ({len(updated)} items)",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            },
        )

    return write_todos
