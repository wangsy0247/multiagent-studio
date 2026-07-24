"""Tests for MemoryMiddleware latest-exchange submission."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from harness.middleware.memory import MemoryMiddleware

THREAD = "test-latest-exchange-thread"
USER = "test-latest-exchange-user"
AGENT = "default"


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def fake_queue(monkeypatch):
    fake = _FakeQueue()
    monkeypatch.setattr("harness.middleware.memory.get_memory_queue", lambda: fake)
    return fake


def _mw() -> MemoryMiddleware:
    return MemoryMiddleware({"memory_enabled": True}, agent_name=AGENT)


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(context={"thread_id": THREAD})


def _state(messages: list) -> dict:
    return {"messages": messages, "user_id": USER}


def _pair(hid: str, aid: str, n: int, **ai_kwargs) -> list:
    return [
        HumanMessage(content=f"user message {n}", id=hid),
        AIMessage(content=f"assistant reply {n}", id=aid, **ai_kwargs),
    ]


def _reminder(mid: str = "rem1") -> HumanMessage:
    return HumanMessage(
        content="<system-reminder>\n<current_date>2026-07-23</current_date>\n</system-reminder>",
        id=mid,
        additional_kwargs={"dynamic_context_reminder": True},
    )


class TestLatestExchange:
    @pytest.mark.asyncio
    async def test_submits_last_human_ai_pair(self, fake_queue):
        await _mw().aafter_agent(_state(_pair("h1", "a1", 1)), _runtime())
        assert len(fake_queue.calls) == 1
        assert [m.id for m in fake_queue.calls[0]["messages"]] == ["h1", "a1"]

    @pytest.mark.asyncio
    async def test_growing_history_submits_only_latest_pair(self, fake_queue):
        mw = _mw()
        msgs = _pair("h1", "a1", 1)
        await mw.aafter_agent(_state(msgs), _runtime())
        msgs = msgs + _pair("h2", "a2", 2)
        await mw.aafter_agent(_state(msgs), _runtime())
        assert len(fake_queue.calls) == 2
        assert [m.id for m in fake_queue.calls[1]["messages"]] == ["h2", "a2"]

    @pytest.mark.asyncio
    async def test_tool_call_tail_ignored(self, fake_queue):
        """轮次以 tool_calls 消息收尾时, 仍取最后一条最终回复配对."""
        tool_ai = AIMessage(
            content="", id="ai_tool",
            tool_calls=[{"name": "file_read", "args": {"path": "/x"}, "id": "tc1"}],
        )
        await _mw().aafter_agent(_state(_pair("h1", "a1", 1) + [tool_ai]), _runtime())
        assert [m.id for m in fake_queue.calls[0]["messages"]] == ["h1", "a1"]

    @pytest.mark.asyncio
    async def test_no_human_before_ai_skips(self, fake_queue):
        await _mw().aafter_agent(
            _state([AIMessage(content="orphan reply", id="a0")]), _runtime()
        )
        assert fake_queue.calls == []


class TestFinishReason:
    @pytest.mark.asyncio
    async def test_stop_submits(self, fake_queue):
        await _mw().aafter_agent(
            _state(_pair("h1", "a1", 1, response_metadata={"finish_reason": "stop"})),
            _runtime(),
        )
        assert len(fake_queue.calls) == 1

    @pytest.mark.asyncio
    async def test_missing_finish_reason_submits(self, fake_queue):
        """provider 未上报 finish_reason 时视为正常完成."""
        await _mw().aafter_agent(_state(_pair("h1", "a1", 1)), _runtime())
        assert len(fake_queue.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls"])
    async def test_truncated_reply_skipped(self, fake_queue, reason):
        await _mw().aafter_agent(
            _state(_pair("h1", "a1", 1, response_metadata={"finish_reason": reason})),
            _runtime(),
        )
        assert fake_queue.calls == []


class TestSummaryAndReminder:
    @pytest.mark.asyncio
    async def test_summary_message_excluded(self, fake_queue):
        """压缩后的状态: 摘要消息不进入提交, 只提交最新真实交换."""
        state = [
            HumanMessage(content="Here is a summary ...", id="sum1", name="summary"),
            AIMessage(content="assistant reply 1", id="a1"),
            *_pair("h2", "a2", 2),
        ]
        await _mw().aafter_agent(_state(state), _runtime())
        submitted = fake_queue.calls[0]["messages"]
        assert [m.id for m in submitted] == ["h2", "a2"]
        assert all(getattr(m, "name", None) != "summary" for m in submitted)

    @pytest.mark.asyncio
    async def test_leading_reminder_included(self, fake_queue):
        """第一条隐藏的日期/记忆注入消息随最新交换一并提交."""
        state = [_reminder()] + _pair("h1", "a1", 1) + _pair("h2", "a2", 2)
        await _mw().aafter_agent(_state(state), _runtime())
        submitted = fake_queue.calls[0]["messages"]
        assert [m.id for m in submitted] == ["rem1", "h2", "a2"]

    @pytest.mark.asyncio
    async def test_reminder_not_duplicated_when_only_human(self, fake_queue):
        """提醒消息本身就是最后一条 human 时不重复添加."""
        state = [_reminder(), AIMessage(content="reply", id="a1")]
        await _mw().aafter_agent(_state(state), _runtime())
        submitted = fake_queue.calls[0]["messages"]
        assert [m.id for m in submitted] == ["rem1", "a1"]
