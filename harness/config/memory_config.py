"""Configuration for memory mechanism — adapted from DeerFlow.

Supports two backends:
- ``file`` — legacy JSON-based FileMemoryStorage (default)
- ``mem0`` — mem0 + Chroma vector store with per-turn search + injection
"""

from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """Configuration for global memory mechanism."""

    enabled: bool = Field(default=True, description="Whether to enable memory mechanism")
    storage_path: str = Field(
        default="",
        description="Path to store memory data. If empty, defaults to ~/.multiagent-studio/memory",
    )
    storage_class: str = Field(
        default="harness.memory.storage.FileMemoryStorage",
        description="The class path for memory storage provider",
    )
    debounce_seconds: int = Field(
        default=30, ge=1, le=300,
        description="Seconds to wait before processing queued updates (debounce)",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for memory updates (None = use default model)",
    )
    api_key: str = Field(
        default="",
        description="API key for memory model (空字符串回退到 OPENAI_API_KEY 环境变量)",
    )
    base_url: str = Field(
        default="",
        description="Base URL for memory model (空字符串回退到 OPENAI_BASE_URL 环境变量)",
    )
    max_facts: int = Field(
        default=100, ge=10, le=500,
        description="Maximum number of facts to store (newest N retained when exceeded)",
    )
    memory_ttl_days: int = Field(
        default=90, ge=0, le=730,
        description="事实过期天数。0 = 永不过期。超过此天数的 facts 会被自动清理",
    )
    fact_confidence_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="Minimum confidence threshold for storing facts",
    )
    injection_enabled: bool = Field(
        default=True,
        description="Whether to inject memory into system prompt",
    )
    max_injection_tokens: int = Field(
        default=2000, ge=100, le=8000,
        description="Maximum tokens to use for memory injection",
    )

    # ── mem0 配置（新增）──────────────────────────────────────────────
    backend: str = Field(
        default="file",
        description="Memory backend: 'file' (legacy JSON) or 'mem0' (mem0+vector store)",
    )
    mem0_config: dict = Field(
        default_factory=dict,
        description="mem0 configuration dict, see mem0 docs. Only used when backend='mem0'",
    )
    mem0_search_top_k: int = Field(
        default=5, ge=1, le=20,
        description="Number of memories to retrieve per search",
    )
    mem0_general_query: str = Field(
        default="用户的偏好、习惯、背景和重要信息",
        description="Fixed query for retrieving general user memories on first turn",
    )
    mem0_enable_time_filter: bool = Field(
        default=False,
        description="Whether to filter memories by created_at recency",
    )
    mem0_recent_days: int = Field(
        default=90, ge=1, le=365,
        description="Only retrieve memories created within this many days (when time filter enabled)",
    )
    mem0_general_token_budget: int = Field(
        default=400, ge=100, le=4000,
        description="Token budget for fixed general query results on first turn. Remainder of max_injection_tokens goes to specific query.",
    )
    mem0_tool_enabled: bool = Field(
        default=False,
        description=(
            "Whether to register memory_search tool for Agent to proactively query mem0. "
            "Independent of backend — can be True even when backend='file', "
            "enabling dual-track: file for passive injection + mem0 for active query."
        ),
    )

    # ── 项目记忆 (仅 Team 模式) ──
    project_memory_enabled: bool = Field(
        default=True,
        description="Whether to load project memory (description.md) in team mode",
    )
    project_memory_root: str = Field(
        default="",
        description=(
            "Root directory for project memory files. "
            "When empty, defaults to the project's git root. "
            "Only used in team mode (single-agent mode does not load project memory)."
        ),
    )


# Global configuration instance
_memory_config: MemoryConfig = MemoryConfig()


def get_memory_config() -> MemoryConfig:
    return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    global _memory_config
    _memory_config = config
