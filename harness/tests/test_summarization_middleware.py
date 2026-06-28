"""Tests for SummarizationMiddleware dynamic-context reminder protection."""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage

from harness.middleware.dynamic_context import is_dynamic_context_reminder
from harness.middleware.summarization import SummarizationMiddleware

os.environ.setdefault("OPENAI_API_KEY", "sk-test")


@pytest.fixture
def dynamic_context_reminder() -> HumanMessage:
    return HumanMessage(
        content="<system-reminder>\n<current_date>2026-06-27, Saturday</current_date>\n</system-reminder>",
        additional_kwargs={"dynamic_context_reminder": True},
    )


@pytest.fixture
def user_message() -> HumanMessage:
    return HumanMessage(content="hello")


class TestPreserveDynamicContextReminders:
    """Dynamic-context reminders should survive summarization when enabled."""

    def test_enabled_moves_reminders_to_preserved(
        self,
        dynamic_context_reminder: HumanMessage,
        user_message: HumanMessage,
    ) -> None:
        mw = SummarizationMiddleware(
            model="gpt-4o-mini",
            trigger=("messages", 3),
            keep=("messages", 1),
            preserve_dynamic_context_reminders=True,
        )

        to_summarize = [dynamic_context_reminder, user_message]
        remaining, preserved = mw._preserve_dynamic_context_reminders(to_summarize, [])

        assert len(remaining) == 1
        assert remaining[0] is user_message
        assert len(preserved) == 1
        assert preserved[0] is dynamic_context_reminder
        assert is_dynamic_context_reminder(preserved[0])

    def test_disabled_leaves_reminders_in_summarize_set(
        self,
        dynamic_context_reminder: HumanMessage,
        user_message: HumanMessage,
    ) -> None:
        mw = SummarizationMiddleware(
            model="gpt-4o-mini",
            trigger=("messages", 3),
            keep=("messages", 1),
            preserve_dynamic_context_reminders=False,
        )

        to_summarize = [dynamic_context_reminder, user_message]
        remaining, preserved = mw._preserve_dynamic_context_reminders(to_summarize, [])

        assert len(remaining) == 2
        assert len(preserved) == 0

    def test_no_reminders_is_no_op(
        self,
        user_message: HumanMessage,
    ) -> None:
        mw = SummarizationMiddleware(
            model="gpt-4o-mini",
            trigger=("messages", 3),
            keep=("messages", 1),
            preserve_dynamic_context_reminders=True,
        )

        to_summarize = [user_message]
        remaining, preserved = mw._preserve_dynamic_context_reminders(to_summarize, [])

        assert remaining == to_summarize
        assert preserved == []
