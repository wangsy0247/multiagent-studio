"""Tests for /compact and /clear slash commands."""

from __future__ import annotations

import os

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from harness.main import parse_slash_command
from harness.middleware.summarization import SummarizationMiddleware

os.environ.setdefault("OPENAI_API_KEY", "sk-test")


def _msgs(n_pairs: int) -> list:
    out = []
    for i in range(n_pairs):
        out.append(HumanMessage(content=f"user {i}", id=f"h{i}"))
        out.append(AIMessage(content=f"assistant {i}", id=f"a{i}"))
    return out


def _reminder() -> HumanMessage:
    return HumanMessage(
        content="<system-reminder>\n<current_date>2026-07-23</current_date>\n</system-reminder>",
        id="rem1",
        additional_kwargs={"dynamic_context_reminder": True},
    )


def _mw(keep: int = 2) -> SummarizationMiddleware:
    return SummarizationMiddleware(
        model=GenericFakeChatModel(messages=iter(["FAKE SUMMARY"])),
        trigger=("tokens", 20000),
        keep=("messages", keep),
    )


class TestParseSlashCommand:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/compact", "/compact"),
            ("/clear", "/clear"),
            ("  /Compact  ", "/compact"),
            ("/CLEAR", "/clear"),
            ("hello", None),
            ("", None),
            ("/compact 顺便总结一下", None),  # 仅精确匹配, 带参数不识别
            ("/unknown", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert parse_slash_command(raw) == expected


class TestForceSummarize:
    @pytest.mark.asyncio
    async def test_compresses_long_history(self):
        update = await _mw(keep=2).aforce_summarize(_msgs(3))  # 6 条消息
        assert update is not None
        msgs = update["messages"]
        # RemoveMessage 占位 + 摘要 HumanMessage + 保留尾部 2 条
        assert isinstance(msgs[0], RemoveMessage)
        assert msgs[1].name == "summary"
        assert "FAKE SUMMARY" in msgs[1].content
        assert [m.id for m in msgs[2:]] == ["h2", "a2"]

    @pytest.mark.asyncio
    async def test_short_history_returns_none(self):
        assert await _mw(keep=10).aforce_summarize(_msgs(2)) is None

    @pytest.mark.asyncio
    async def test_dynamic_context_reminder_preserved(self):
        update = await _mw(keep=2).aforce_summarize([_reminder()] + _msgs(3))
        assert update is not None
        ids = [m.id for m in update["messages"]]
        assert "rem1" in ids  # 提醒消息不被压缩, 保留在结果中

    @pytest.mark.asyncio
    async def test_ignores_trigger_threshold(self):
        """force 路径不受 trigger 阈值影响 (远低于 20000 token 也会压缩)."""
        update = await _mw(keep=2).aforce_summarize(_msgs(3))
        assert update is not None

    @pytest.mark.asyncio
    async def test_none_summary_prompt_falls_back_to_default(self):
        """工厂会传 summary_prompt=None (SummarizationConfig 默认值) —
        构造时必须回退到父类默认 prompt, 否则 None.format() 报错且被父类吞成摘要文本."""
        mw = SummarizationMiddleware(
            model=GenericFakeChatModel(messages=iter(["FAKE SUMMARY"])),
            trigger=("tokens", 20000),
            keep=("messages", 2),
            summary_prompt=None,
        )
        update = await mw.aforce_summarize(_msgs(3))
        assert update is not None
        summary_msg = update["messages"][1]
        assert "FAKE SUMMARY" in summary_msg.content
        assert "Error generating summary" not in summary_msg.content
