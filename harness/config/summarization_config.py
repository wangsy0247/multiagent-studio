"""Configuration for conversation summarization — adapted from DeerFlow."""

from typing import Literal

from pydantic import BaseModel, Field

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
    summary_prompt: str | None = Field(default=None, description="Custom prompt for summaries")
    preserve_dynamic_context_reminders: bool = Field(
        default=True,
        description="Keep hidden dynamic-context reminders out of summary compression",
    )
    preserve_recent_skill_count: int = Field(default=5, ge=0)
    preserve_recent_skill_tokens: int = Field(default=25000, ge=0)
    preserve_recent_skill_tokens_per_skill: int = Field(default=5000, ge=0)
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: ["read_file", "read", "view", "cat"],
    )


_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    global _summarization_config
    _summarization_config = config
