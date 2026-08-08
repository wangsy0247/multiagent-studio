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


class TestCreateSummarizationMiddleware:
    """工厂函数: fraction 触发需要模型 profile, 自定义模型必须显式注入."""

    @pytest.fixture
    def fraction_config(self):
        from harness.config.summarization_config import (
            ContextSize,
            SummarizationConfig,
            set_summarization_config,
        )
        cfg = SummarizationConfig(
            enabled=True,
            trigger=[ContextSize(type="fraction", value=0.7)],
            keep=ContextSize(type="messages", value=20),
        )
        set_summarization_config(cfg)
        yield cfg
        set_summarization_config(SummarizationConfig())

    def test_fraction_trigger_with_unknown_model(self, fraction_config):
        """qwen 等自定义模型不在 langchain 内置 profile 表 — 注入
        max_input_tokens 后 fraction 触发必须能正常构建."""
        from harness.middleware.summarization import create_summarization_middleware

        mw = create_summarization_middleware(model_name="qwen3.6-plus")
        assert mw is not None
        assert mw.model.profile is not None
        assert mw.model.profile["max_input_tokens"] == fraction_config.max_input_tokens

    def test_construction_failure_degrades_to_none(self, fraction_config, monkeypatch):
        """中间件构造失败只禁用压缩, 绝不允许拖垮整个运行."""
        import harness.middleware.summarization as summ_module

        class ExplodingMiddleware:
            def __init__(self, *args, **kwargs):
                raise ValueError("langchain behavior changed")

        monkeypatch.setattr(summ_module, "SummarizationMiddleware", ExplodingMiddleware)
        mw = summ_module.create_summarization_middleware(model_name="qwen3.6-plus")
        assert mw is None

    def test_max_input_tokens_parsed_from_dict(self):
        from harness.config.summarization_config import load_summarization_config_from_dict

        cfg = load_summarization_config_from_dict({
            "trigger": [{"type": "fraction", "value": 0.7}],
            "max_input_tokens": 64000,
        })
        assert cfg.max_input_tokens == 64000

        default_cfg = load_summarization_config_from_dict(None)
        assert default_cfg.max_input_tokens == 128_000
