"""Extensions configuration — MCP servers and skills enabled state.

Adapted from the reference extensions-config design.
Reads and writes ``extensions_config.json``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-file path resolution
# ---------------------------------------------------------------------------

_EXTENSIONS_CONFIG_FILENAME = "extensions_config.json"


def _default_config_path() -> Path:
    """Resolve the extensions config JSON path.

    1. ``EXTENSIONS_CONFIG_PATH`` environment variable.
    2. ``extensions_config.json`` in the current working directory.
    3. ``extensions_config.json`` next to the harness package (fallback for
       when the server is started from the project root).
    """
    if env := os.getenv("EXTENSIONS_CONFIG_PATH"):
        return Path(env)

    cwd_candidate = Path.cwd() / _EXTENSIONS_CONFIG_FILENAME
    if cwd_candidate.exists():
        return cwd_candidate

    # Fallback: resolve relative to the harness package directory.
    # 规范位置是项目根目录 (start.sh 从项目根启动, CWD 即根目录);
    # 此 fallback 兼容从 harness/ 目录内启动的场景。
    harness_dir = Path(__file__).resolve().parent.parent
    harness_candidate = harness_dir / _EXTENSIONS_CONFIG_FILENAME
    if harness_candidate.exists():
        return harness_candidate

    return cwd_candidate


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class McpOAuthConfig(BaseModel):
    """OAuth configuration for an MCP server (HTTP/SSE transports)."""

    enabled: bool = Field(default=True, description="Whether OAuth token injection is enabled")
    token_url: str = Field(default="", description="OAuth token endpoint URL")
    grant_type: Literal["client_credentials", "refresh_token"] = Field(
        default="client_credentials",
        description="OAuth grant type",
    )
    client_id: str | None = Field(default=None, description="OAuth client ID")
    client_secret: str | None = Field(default=None, description="OAuth client secret")
    refresh_token: str | None = Field(default=None, description="OAuth refresh token (for refresh_token grant)")
    scope: str | None = Field(default=None, description="OAuth scope")
    audience: str | None = Field(default=None, description="OAuth audience (provider-specific)")
    token_field: str = Field(default="access_token", description="Field name containing access token in token response")
    token_type_field: str = Field(default="token_type", description="Field name containing token type in token response")
    expires_in_field: str = Field(default="expires_in", description="Field name containing expiry (seconds) in token response")
    default_token_type: str = Field(default="Bearer", description="Default token type when missing in token response")
    refresh_skew_seconds: int = Field(default=60, description="Refresh token this many seconds before expiry")
    extra_token_params: dict[str, str] = Field(default_factory=dict, description="Additional form params sent to token endpoint")
    model_config = ConfigDict(extra="allow")


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    enabled: bool = Field(default=True, description="Whether this MCP server is enabled")
    type: str = Field(default="stdio", description="Transport type: 'stdio', 'sse', or 'http'")
    command: str | None = Field(default=None, description="Command to execute to start the MCP server (for stdio type)")
    args: list[str] = Field(default_factory=list, description="Arguments to pass to the command (for stdio type)")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the MCP server")
    url: str | None = Field(default=None, description="URL of the MCP server (for sse or http type)")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP headers to send (for sse or http type)")
    oauth: McpOAuthConfig | None = Field(default=None, description="OAuth configuration (for sse or http type)")
    description: str = Field(default="", description="Human-readable description of what this MCP server provides")
    model_config = ConfigDict(extra="allow")


class SkillStateConfig(BaseModel):
    """Per-skill enabled/disabled state."""

    enabled: bool = True


class ExtensionsConfig(BaseModel):
    """Runtime extension configuration persisted as JSON.

    * ``mcp_servers`` — MCP server definitions keyed by server name.
    * ``skills`` — per-skill enabled-state map keyed by skill name.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(
        default_factory=dict,
        description="Map of MCP server name to configuration",
        alias="mcpServers",
    )
    skills: dict[str, SkillStateConfig] = Field(
        default_factory=dict,
        description="Map of skill name to state configuration",
    )

    # ------------------------------------------------------------------
    # class methods
    # ------------------------------------------------------------------

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path | None:
        """Resolve the extensions config file path.

        Priority:
        1. If provided ``config_path`` argument, use it.
        2. If provided ``EXTENSIONS_CONFIG_PATH`` environment variable, use it.
        3. Otherwise, search current directory for ``extensions_config.json``.

        Returns:
            Path to the extensions config file if found, otherwise None.
        """
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(
                    f"Extensions config file specified by param `config_path` not found at {path}"
                )
            return path
        elif env := os.getenv("EXTENSIONS_CONFIG_PATH"):
            path = Path(env)
            if not path.exists():
                raise FileNotFoundError(
                    f"Extensions config file specified by environment variable "
                    f"`EXTENSIONS_CONFIG_PATH` not found at {path}"
                )
            return path
        else:
            path = _default_config_path()
            if path.exists():
                return path
            return None

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> ExtensionsConfig:
        """Load extensions configuration from a JSON file.

        Returns a default (empty) config when the file does not exist or
        cannot be parsed.
        """
        if path:
            config_path = Path(path)
            # If the explicit path doesn't exist, try the default search.
            if not config_path.is_absolute() and not config_path.exists():
                fallback = _default_config_path()
                if fallback.exists():
                    logger.debug(
                        "Extensions config: '%s' not found, using fallback %s",
                        path, fallback,
                    )
                    config_path = fallback
        else:
            config_path = cls.resolve_config_path() or _default_config_path()
        if not config_path.exists():
            logger.debug("Extensions config not found at %s, using defaults", config_path)
            return cls()

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read extensions config %s: %s — using defaults",
                config_path,
                exc,
            )
            return cls()

        # Resolve environment variables in string values.
        raw = cls.resolve_env_variables(raw)

        # Normalise: skills values can be plain bools or {enabled: bool} objects.
        skills_raw = raw.get("skills", {})
        if isinstance(skills_raw, dict):
            normalised: dict[str, SkillStateConfig] = {}
            for name, value in skills_raw.items():
                if isinstance(value, dict):
                    normalised[name] = SkillStateConfig(**value)
                elif isinstance(value, bool):
                    normalised[name] = SkillStateConfig(enabled=value)
                else:
                    normalised[name] = SkillStateConfig()
            raw["skills"] = normalised

        return cls.model_validate(raw)

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """Recursively resolve environment variables in strings.

        Strings starting with ``$`` are treated as environment variable names.
        Example: ``$OPENAI_API_KEY`` → ``os.getenv("OPENAI_API_KEY")``.
        """
        if isinstance(config, str):
            if not config.startswith("$"):
                return config
            env_value = os.getenv(config[1:])
            if env_value is None:
                return ""
            return env_value

        if isinstance(config, dict):
            return {key: cls.resolve_env_variables(value) for key, value in config.items()}

        if isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]

        if isinstance(config, tuple):
            return tuple(cls.resolve_env_variables(item) for item in config)

        return config

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        """Get only the enabled MCP servers."""
        return {name: config for name, config in self.mcp_servers.items() if config.enabled}

    def is_skill_enabled(
        self, skill_name: str, skill_category: str
    ) -> bool:
        """Return ``True`` when *skill_name* is enabled.

        Defaults to ``True`` when no explicit state is configured.
        """
        state = self.skills.get(skill_name)
        if state is not None:
            return state.enabled
        return True


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------

_extensions_config: ExtensionsConfig | None = None


def get_extensions_config() -> ExtensionsConfig:
    """Get the cached extensions config singleton.

    Loads from file on first access.
    """
    global _extensions_config
    if _extensions_config is None:
        _extensions_config = ExtensionsConfig.from_file()
    return _extensions_config


def reload_extensions_config(config_path: str | None = None) -> ExtensionsConfig:
    """Reload extensions config from file and update the cached instance."""
    global _extensions_config
    _extensions_config = ExtensionsConfig.from_file(config_path)
    return _extensions_config


def reset_extensions_config() -> None:
    """Reset the cached extensions config (for tests)."""
    global _extensions_config
    _extensions_config = None


def set_extensions_config(config: ExtensionsConfig) -> None:
    """Inject a custom extensions config (for tests)."""
    global _extensions_config
    _extensions_config = config
