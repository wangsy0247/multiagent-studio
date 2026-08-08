"""Configuration for conversation summarization — adapted from harness."""

import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ContextSizeType = Literal["fraction", "tokens", "messages"]


class ContextSize(BaseModel):
    """Context size specification for trigger or keep parameters."""
    type: ContextSizeType = Field(description="Type of context size specification")
    value: int | float = Field(description="Value for the context size specification")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """Configuration for automatic conversation summarization."""

    enabled: bool = Field(default=True, description="Whether to enable summarization")
    model_name: str | None = Field(default=None, description="Model for summarization (None = lightweight default)")
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="Threshold(s) that trigger summarization (OR semantics across list items)",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="Context retention policy after summarization",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Max tokens when preparing messages for summarization (None = skip trimming)",
    )
    max_input_tokens: int = Field(
        default=128_000,
        description=(
            "模型最大输入 token 数 — fraction 类型的 trigger/keep 以此为基数换算。"
            "langchain 新版对 fraction 限制强制要求模型 profile, 自定义模型 "
            "(如 qwen/dashscope) 不在内置 profile 表中, 必须显式注入。"
        ),
    )
    summary_prompt: str | None = Field(default=None, description="Custom prompt for summaries")
    preserve_dynamic_context_reminders: bool = Field(
        default=True,
        description="Keep hidden dynamic-context reminders out of summary compression",
    )
    preserve_recent_skill_count: int = Field(default=5, ge=0)
    preserve_recent_skill_tokens: int = Field(default=25000, ge=0)
    preserve_recent_skill_tokens_per_skill: int = Field(default=5000, ge=0)
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: ["file_read", "read_file", "read", "view", "cat"],
    )


_summarization_config: SummarizationConfig = SummarizationConfig()


def load_summarization_config_from_dict(data: dict | None) -> SummarizationConfig:
    """从 config.yaml 的 summarization 小节构造 SummarizationConfig.

    兼容两种形式:
      - 嵌套 (config.yaml):  ``trigger: [{type: tokens, value: 20000}]`` /
        ``keep: {type: messages, value: 10}``
      - 扁平 (SYSTEM_DEFAULTS / SummarizationGlobalConfig):
        ``trigger_tokens: 20000`` / ``keep_messages: 10``

    非法输入回退到默认值 (trigger=None, 即不触发).
    """
    if not isinstance(data, dict):
        return SummarizationConfig()
    payload = {k: v for k, v in data.items() if k in SummarizationConfig.model_fields}
    if "trigger" not in payload and data.get("trigger_tokens"):
        payload["trigger"] = {"type": "tokens", "value": data["trigger_tokens"]}
    if "keep" not in payload and data.get("keep_messages"):
        payload["keep"] = {"type": "messages", "value": data["keep_messages"]}
    try:
        return SummarizationConfig(**payload)
    except Exception:
        logger.warning("Invalid summarization config %r, using defaults", data)
        return SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    global _summarization_config
    _summarization_config = config
