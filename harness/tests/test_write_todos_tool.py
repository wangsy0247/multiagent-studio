"""Tests for the write_todos tool (plan-mode TODO producer)."""

from __future__ import annotations

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from harness.models import TodoItem
from harness.tools.builtins.write_todos_tool import write_todos_tool


def _runtime(todos: list | None = None) -> ToolRuntime:
    return ToolRuntime(
        state={"todos": todos or []},
        context=None,
        config={},
        stream_writer=lambda *a, **k: None,
        tool_call_id="tc-1",
        store=None,
    )


async def _call(tool, todos: list[dict], runtime) -> Command:
    return await tool.ainvoke({"todos": todos, "runtime": runtime})


class TestWriteTodosTool:
    def test_schema_hides_runtime(self) -> None:
        tool = write_todos_tool()
        assert tool.name == "write_todos"
        assert list(tool.args) == ["todos"]

    @pytest.mark.asyncio
    async def test_create_new_items_with_generated_ids(self) -> None:
        tool = write_todos_tool()
        result = await _call(
            tool,
            [
                {"description": "step 1", "status": "in_progress"},
                {"description": "step 2"},
            ],
            _runtime(),
        )
        assert isinstance(result, Command)
        items = result.update["todos"]
        assert len(items) == 2
        assert all(isinstance(t, TodoItem) for t in items)
        assert items[0].description == "step 1"
        assert items[0].status == "in_progress"
        assert items[1].status == "pending"
        assert all(t.id for t in items)
        # ToolMessage 回执
        assert "Updated todo list" in result.update["messages"][0].content

    @pytest.mark.asyncio
    async def test_update_by_id(self) -> None:
        existing = TodoItem(description="step 1", status="pending")
        tool = write_todos_tool()
        result = await _call(
            tool,
            [{"id": existing.id, "description": "step 1", "status": "completed"}],
            _runtime([existing]),
        )
        items = result.update["todos"]
        assert len(items) == 1
        assert items[0].id == existing.id
        assert items[0].status == "completed"
        assert items[0].completed_at is not None

    @pytest.mark.asyncio
    async def test_update_by_description_idempotent(self) -> None:
        """模型每轮发全量列表 (无 id) 时按 description 幂等匹配, 不产生重复项。"""
        existing = TodoItem(description="step 1", status="pending")
        tool = write_todos_tool()
        result = await _call(
            tool,
            [
                {"description": "step 1", "status": "completed"},
                {"description": "step 2", "status": "in_progress"},
            ],
            _runtime([existing]),
        )
        items = result.update["todos"]
        assert items[0].id == existing.id  # 复用旧 id → reducer 归并后不重复
        assert items[0].status == "completed"
        assert items[1].description == "step 2"

    @pytest.mark.asyncio
    async def test_empty_description_rejected(self) -> None:
        tool = write_todos_tool()
        result = await _call(
            tool, [{"description": "  "}], _runtime()
        )
        assert "todos" not in result.update
        assert "Error" in result.update["messages"][0].content

    @pytest.mark.asyncio
    async def test_state_todos_as_dicts(self) -> None:
        """checkpoint 恢复后 state['todos'] 可能是 dict 而非 TodoItem。"""
        existing = TodoItem(description="step 1", status="pending")
        tool = write_todos_tool()
        result = await _call(
            tool,
            [{"description": "step 1", "status": "completed"}],
            _runtime([existing.model_dump()]),
        )
        assert result.update["todos"][0].id == existing.id
