"""Tests for ClarificationMiddleware options parsing and interruption."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from harness.middleware.clarification import ClarificationMiddleware
from harness.models import HarnessState


def _make_request(*, name: str = "ask_clarification", args: dict, tool_call_id: str = "tc-1"):
    return ToolCallRequest(
        tool_call={
            "id": tool_call_id,
            "name": name,
            "args": args,
        },
        tool=None,
        state=None,
        runtime=None,
    )


@pytest.mark.asyncio
async def test_options_json_string_is_parsed():
    """LLM may return options as a JSON string instead of a list."""
    mw = ClarificationMiddleware()
    request = _make_request(
        args={
            "question": "请选择下一步",
            "options": '["生成 ABACUS 输入", "查看 POSCAR 文件的差异"]',
            "required": True,
        }
    )

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    assert isinstance(result.update["pending_clarification"].options, list)
    assert result.update["pending_clarification"].options == [
        "生成 ABACUS 输入",
        "查看 POSCAR 文件的差异",
    ]


@pytest.mark.asyncio
async def test_options_list_passes_through():
    """Normal list options should still work."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "请选择", "options": ["A", "B"]})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    assert result.update["pending_clarification"].options == ["A", "B"]


@pytest.mark.asyncio
async def test_invalid_options_string_becomes_list():
    """Malformed JSON string should be wrapped as a single-item list."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "请选择", "options": "not valid json"})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    assert result.update["pending_clarification"].options == ["not valid json"]


@pytest.mark.asyncio
async def test_clarification_interruption_goes_to_end():
    """ask_clarification should interrupt execution with goto=END."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "确认继续？", "required": True})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    assert result.goto == "__end__"
    assert result.update["is_finished"] is True
    assert result.update["pending_clarification"].question == "确认继续？"
    assert len(result.update["messages"]) == 1
    assert result.update["messages"][0].content.startswith("❓")


@pytest.mark.asyncio
async def test_non_clarification_tool_passes_through():
    """Other tools should be executed normally."""
    mw = ClarificationMiddleware()
    request = _make_request(name="bash", args={"command": "ls"})
    handler = AsyncMock(return_value=MagicMock())

    await mw.awrap_tool_call(request, handler)

    handler.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_clarification_answer_injection_handled_by_worker():
    """Clarification answer injection is handled by the worker layer, not middleware.

    The ClarificationMiddleware only intercepts ask_clarification tool calls
    via wrap_tool_call. Answer injection is managed by:
      - main.py::respond_to_clarification() — sets pending_clarification.answer
      - The worker then invokes the graph with the updated state
    """
    # This behavior is tested in test_api.py via the /clarification endpoint
    pass
