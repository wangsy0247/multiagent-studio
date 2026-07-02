"""Tests for ClarificationMiddleware options parsing and interruption."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from harness.middleware.clarification import (
    ClarificationMiddleware,
    extract_clarification_from_tool_message,
    get_pending_clarification,
)


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

    metadata = result.update["messages"][0].additional_kwargs["clarification"]
    assert isinstance(metadata["options"], list)
    assert metadata["options"] == [
        "生成 ABACUS 输入",
        "查看 POSCAR 文件的差异",
    ]


@pytest.mark.asyncio
async def test_options_list_passes_through():
    """Normal list options should still work."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "请选择", "options": ["A", "B"]})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    metadata = result.update["messages"][0].additional_kwargs["clarification"]
    assert metadata["options"] == ["A", "B"]


@pytest.mark.asyncio
async def test_invalid_options_string_becomes_list():
    """Malformed JSON string should be wrapped as a single-item list."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "请选择", "options": "not valid json"})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    metadata = result.update["messages"][0].additional_kwargs["clarification"]
    assert metadata["options"] == ["not valid json"]


@pytest.mark.asyncio
async def test_clarification_interruption_goes_to_end():
    """ask_clarification should interrupt execution with goto=END."""
    mw = ClarificationMiddleware()
    request = _make_request(args={"question": "确认继续？", "required": True})

    result = await mw.awrap_tool_call(request, lambda req: MagicMock())

    assert result.goto == "__end__"
    assert "messages" in result.update
    assert len(result.update["messages"]) == 1
    tool_message = result.update["messages"][0]
    assert tool_message.content.startswith("❓")
    metadata = tool_message.additional_kwargs["clarification"]
    assert metadata["question"] == "确认继续？"
    assert metadata["required"] is True


@pytest.mark.asyncio
async def test_non_clarification_tool_passes_through():
    """Other tools should be executed normally."""
    mw = ClarificationMiddleware()
    request = _make_request(name="bash", args={"command": "ls"})
    handler = AsyncMock(return_value=MagicMock())

    await mw.awrap_tool_call(request, handler)

    handler.assert_called_once_with(request)


def test_extract_clarification_from_tool_message():
    """Metadata can be recovered from an ask_clarification ToolMessage."""
    from langchain_core.messages import ToolMessage

    tool_message = ToolMessage(
        content="❓ Which language?",
        name="ask_clarification",
        tool_call_id="tc-1",
        additional_kwargs={
            "clarification": {
                "question": "Which language?",
                "options": ["Python", "Go"],
                "required": True,
            }
        },
    )
    metadata = extract_clarification_from_tool_message(tool_message)
    assert metadata["question"] == "Which language?"
    assert metadata["options"] == ["Python", "Go"]
    assert metadata["required"] is True


def test_get_pending_clarification_detects_unanswered_question():
    """A pending clarification is detected when no human message follows it."""
    messages = [
        HumanMessage(content="write a script"),
        AIMessage(content="", tool_calls=[{"name": "ask_clarification", "args": {"question": "Which language?"}, "id": "tc-1"}]),
        ToolMessage(
            content="❓ Which language?",
            name="ask_clarification",
            tool_call_id="tc-1",
            additional_kwargs={"clarification": {"question": "Which language?", "options": []}},
        ),
    ]
    pending = get_pending_clarification(messages)
    assert pending is not None
    assert pending["question"] == "Which language?"


def test_get_pending_clarification_returns_none_after_answer():
    """Once the user answers, there is no pending clarification."""
    messages = [
        HumanMessage(content="write a script"),
        AIMessage(content="", tool_calls=[{"name": "ask_clarification", "args": {"question": "Which language?"}, "id": "tc-1"}]),
        ToolMessage(content="❓ Which language?", name="ask_clarification", tool_call_id="tc-1"),
        HumanMessage(content="Python"),
    ]
    pending = get_pending_clarification(messages)
    assert pending is None
