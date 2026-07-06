"""Extensions configuration — MCP servers and skills enabled state.

Adapted from DeerFlow's ``deerflow.config.extensions_config``, simplified
for multiagent-studio's JSON-file pattern.  Reads and writes
``extensions_config.json``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-file path resolution
# ---------------------------------------------------------------------------

_EXTENSIONS_CONFIG_FILENAME = "extensions_config.json"


def _default_config_path() -> Path:
    """Resolve the extensions config JSON path.

    1. ``EXTENSIONS_CONFIG_PATH`` environment variable.
    2. ``extensions_config.json`` in the current working directory.
    """
    if env := os.getenv("EXTENSIONS_CONFIG_PATH"):
        return Path(env)
    return Path.cwd() / _EXTENSIONS_CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SkillStateConfig(BaseModel):
    """Per-skill enabled/disabled state."""

    enabled: bool = True


class ExtensionsConfig(BaseModel):
    """Runtime extension configuration persisted as JSON.

    * ``mcp_servers`` — MCP server definitions (preserved as-is for the MCP
      adapter).
    * ``skills`` — per-skill enabled-state map keyed by skill name.
    """

    mcp_servers: dict[str, Any] = Field(default_factory=dict)
    skills: dict[str, SkillStateConfig] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> ExtensionsConfig:
        """Load extensions configuration from a JSON file.

        Returns a default (empty) config when the file does not exist or
        cannot be parsed.
        """
        config_path = Path(path) if path else _default_config_path()
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

        return cls(**raw)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def is_skill_enabled(
        self, skill_name: str, skill_category: str
    ) -> bool:
        """Return ``True`` when *skill_name* is enabled.

        Defaults to ``True`` for both ``public`` and ``custom`` skills when
        no explicit state is configured.
        """
        state = self.skills.get(skill_name)
        if state is not None:
            return state.enabled
        # Default: all skills are enabled.
        return True
