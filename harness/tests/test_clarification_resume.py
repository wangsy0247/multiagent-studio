"""Tests for HITL clarification resumption in HarnessService."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from harness.main import HarnessService


def _make_clarification_tool_message(question: str) -> ToolMessage:
    return ToolMessage(
        content=f"❓ {question}",
        name="ask_clarification",
        tool_call_id="tc-1",
        additional_kwargs={
            "clarification": {
                "question": question,
                "context": "",
                "options": [],
                "required": False,
                "clarification_type": "missing_info",
            }
        },
    )


def _install_mock_ctx(service: HarnessService, mock_graph) -> None:
    """respond_to_clarification 经 _get_or_create_graph_context 取 graph —
    测试用 mock ctx 替换 (service.graph 直连的时代已过去)."""
    ctx = MagicMock()
    ctx.graph = mock_graph
    ctx.effective_config = MagicMock()
    service._initialized = True
    service._get_or_create_graph_context = AsyncMock(return_value=ctx)


@pytest.mark.asyncio
async def test_respond_to_clarification_injects_answer_and_resumes():
    """When the user answers a clarification, the answer must appear in the
    message history so the graph can resume instead of looping back to the
    same question.
    """
    service = HarnessService()
    service.observability = None

    state_snapshot = MagicMock()
    state_snapshot.values = {
        "thread_id": "t1",
        "user_id": "u1",
        "messages": [
            HumanMessage(content="write a script"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "ask_clarification", "args": {"question": "Which language?"}, "id": "tc-1"}
                ],
            ),
            _make_clarification_tool_message("Which language?"),
        ],
    }

    mock_graph = MagicMock()
    mock_graph.aget_state = AsyncMock(return_value=state_snapshot)

    captured_state: dict | None = None

    async def mock_astream_events(state, config, version):
        nonlocal captured_state
        captured_state = state
        yield {"event": "on_chain_end", "name": "LangGraph", "data": {"output": {}}}

    mock_graph.astream_events = mock_astream_events
    _install_mock_ctx(service, mock_graph)

    events = []
    async for event in service.respond_to_clarification("t1", "Python"):
        events.append(event)

    assert captured_state is not None
    messages = captured_state["messages"]
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "Python"


@pytest.mark.asyncio
async def test_respond_to_clarification_rejects_when_no_pending_question():
    """If the conversation is not waiting for a clarification, respond returns an error."""
    service = HarnessService()
    service.observability = None

    state_snapshot = MagicMock()
    state_snapshot.values = {
        "thread_id": "t1",
        "user_id": "u1",
        "messages": [
            HumanMessage(content="write a script"),
            HumanMessage(content="Python"),
        ],
    }

    mock_graph = MagicMock()
    mock_graph.aget_state = AsyncMock(return_value=state_snapshot)
    _install_mock_ctx(service, mock_graph)

    events = []
    async for event in service.respond_to_clarification("t1", "Python"):
        events.append(event)

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "no pending clarification" in events[0]["content"]
