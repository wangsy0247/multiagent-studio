"""Harness configuration management.

Provides both the Pydantic-based ``HarnessConfig`` (loaded from env vars
and the ``harness/.env`` file) and the YAML-based ``ConfigManager`` with
mtime hot-reload.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from harness.config.config_manager import ConfigManager
from harness.config.tool_config import ToolConfig, ToolGroupConfig

# Resolve .env relative to the harness package directory — NOT the CWD.
# This fixes ``python -m harness.main`` when started from the project root.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class HarnessConfig(BaseSettings):
    """Harness configuration loaded from environment variables and ``.env``."""

    model_config = ConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_prefix="HARNESS_",
        extra="ignore",
    )

    # Data & Paths
    data_root: str = "~/.multiagent-studio"  # 统一数据根目录
    workspace_root: str = "~/.multiagent-studio/workspace"
    memory_root: str = "~/.multiagent-studio/memory"
    prompts_root: str = "./prompts"

    @model_validator(mode="after")
    def _expand_paths(self) -> "HarnessConfig":
        """Expand ~ to user home directory."""
        for field_name in ("data_root", "workspace_root", "memory_root", "prompts_root"):
            val = getattr(self, field_name)
            if val and val.startswith("~"):
                setattr(self, field_name, os.path.expanduser(val))
        return self

    # LLM
    default_model: str = "gpt-4o"
    summary_model: str = "gpt-4o-mini"
    title_model: str = "gpt-4o-mini"
    judge_model: str = "gpt-4o"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # Middleware
    sandbox_use: str = ""  # e.g. harness.services.docker_sandbox_provider:DockerSandboxProvider
    sandbox_image: str = "python:3.11-slim"
    sandbox_mem_limit: str = "512m"
    sandbox_cpu_quota: int = 100000
    tool_max_retries: int = 3
    summary_token_threshold: int = 8000
    summary_message_threshold: int = 20
    max_concurrent_subagents: int = 3
    debounce_seconds: float = 30.0

    # Tools (DeerFlow-style config-driven tool loading)
    tools: list[ToolConfig] = Field(default_factory=list, description="Available tools loaded from config.yaml")
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list, description="Available tool groups")

    # MCP
    mcp_config_path: str = "./extensions_config.json"

    # Service
    harness_port: int = 8001

    # Observability
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Guardrails
    tool_permissions: dict = {}
    default_tool_policy: str = "allow"


def load_config() -> HarnessConfig:
    return HarnessConfig()


__all__ = [
    "HarnessConfig",
    "ConfigManager",
    "load_config",
]
