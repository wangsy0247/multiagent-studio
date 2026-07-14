"""Harness configuration management.

Provides:
  - HarnessConfig: Pydantic-based env-var configuration (no prefix)
  - ConfigManager:  YAML-based configuration with mtime hot-reload
  - ConfigLoader:   Three-layer config merge (L0 system → L1 user global → L2 agent)
  - EffectiveConfig: Merged runtime config dataclass
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from harness.config.config_manager import ConfigManager
from harness.config.config_loader import ConfigLoader, create_user_configs
from harness.config.config_models import EffectiveConfig
from harness.config.defaults import SYSTEM_DEFAULTS, HARDCODED_OVERRIDES
from harness.config.tool_config import ToolConfig, ToolGroupConfig

# Resolve .env relative to the harness package directory — NOT the CWD.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)


class HarnessConfig(BaseSettings):
    """Harness configuration loaded from environment variables and ``.env``.

    NOTE: Most runtime settings now come from EffectiveConfig (three-layer merge).
    HarnessConfig is kept for infrastructure defaults and env-var overrides.
    """

    model_config = ConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        extra="ignore",
    )

    # Data & Paths — 统一根目录, 子路径由 Paths 类管理
    data_root: str = "~/.multiagent-studio"
    workspace_root: str = "~/.multiagent-studio"
    memory_root: str = "~/.multiagent-studio"
    prompts_root: str = "./prompts"

    @model_validator(mode="after")
    def _expand_paths(self) -> "HarnessConfig":
        for field_name in ("data_root", "workspace_root", "memory_root", "prompts_root"):
            val = getattr(self, field_name)
            if val and val.startswith("~"):
                setattr(self, field_name, os.path.expanduser(val))
        return self

    # Sandbox
    sandbox_server_url: str = "http://localhost:8080"
    sandbox_api_key: str = ""
    sandbox_image: str = "python:3.12"

    # Tool
    tool_max_retries: int = 3
    max_concurrent_subagents: int = 3
    tools: list[ToolConfig] = Field(default_factory=list)
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list)
    mcp_config_path: str = "./extensions_config.json"

    # Service
    port: int = 8001

    # Observability (仅作 env-var fallback — EffectiveConfig 优先)
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
    "ConfigLoader",
    "EffectiveConfig",
    "SYSTEM_DEFAULTS",
    "HARDCODED_OVERRIDES",
    "create_user_configs",
    "load_config",
]
