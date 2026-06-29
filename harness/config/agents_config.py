"""Agent configuration management — SOUL.md, config.yaml for custom agents.

Custom agents are stored per-user under ``{data_root}/users/{user_id}/agents/{name}/``.
Layout:
    {data_root}/users/{user_id}/agents/{name}/
    ├── SOUL.md       ← personality / behavioral definition
    ├── config.yaml   ← model, tool_groups, skills
    └── memory.json   ← per-agent long-term memory (managed by memory system)
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"
AGENT_CONFIG_FILENAME = "config.yaml"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_agent_name(name: str) -> str:
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match: {AGENT_NAME_PATTERN.pattern}")
    return name


class AgentConfig(BaseModel):
    """Configuration for a custom agent."""

    name: str
    display_name: str = ""
    description: str = ""
    model: str = "inherit"
    tool_groups: list[str] = Field(default_factory=list)
    skills: list[str] | None = None
    created_at: str = ""
    updated_at: str = ""


def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """Return the on-disk directory for an agent."""
    paths = get_paths()
    uid = user_id or "default"
    return paths.base_dir / "users" / uid / "agents" / name


def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """Scan and return all custom agents for a user."""
    paths = get_paths()
    uid = user_id or "default"
    agents_root = paths.base_dir / "users" / uid / "agents"

    if not agents_root.exists():
        return []

    agents: list[AgentConfig] = []
    for entry in sorted(agents_root.iterdir()):
        if not entry.is_dir():
            continue
        config_file = entry / AGENT_CONFIG_FILENAME
        if not config_file.exists():
            continue
        try:
            cfg = load_agent_config(entry.name, user_id=user_id)
            if cfg is not None:
                agents.append(cfg)
        except Exception as exc:
            logger.warning("Skipping agent '%s': %s", entry.name, exc)

    agents.sort(key=lambda a: a.name)
    return agents


def load_agent_config(name: str, *, user_id: str | None = None) -> AgentConfig | None:
    """Load an agent's config.yaml."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    config_file = agent_dir / AGENT_CONFIG_FILENAME

    if not config_file.exists():
        return None

    try:
        with open(config_file, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse {config_file}: {e}") from e

    data.setdefault("name", name)
    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}
    return AgentConfig(**data)


def save_agent_config(name: str, cfg: AgentConfig, *, user_id: str | None = None) -> None:
    """Save an agent's config.yaml."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    cfg.updated_at = datetime.now(UTC).isoformat()
    if not cfg.created_at:
        cfg.created_at = cfg.updated_at

    data = cfg.model_dump(exclude_none=True)
    config_file = agent_dir / AGENT_CONFIG_FILENAME
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)


def delete_agent(name: str, *, user_id: str | None = None) -> bool:
    """Delete an agent directory. Returns True if deleted, False if not found."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    if not agent_dir.exists():
        return False
    import shutil
    shutil.rmtree(agent_dir)
    return True


def load_agent_soul(name: str, *, user_id: str | None = None) -> str:
    """Load an agent's SOUL.md content. Returns empty string if not found."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    soul_path = agent_dir / SOUL_FILENAME
    if not soul_path.exists():
        return ""
    return soul_path.read_text(encoding="utf-8").strip()


def save_agent_soul(name: str, content: str, *, user_id: str | None = None) -> None:
    """Save an agent's SOUL.md."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    soul_path = agent_dir / SOUL_FILENAME
    soul_path.write_text(content.strip() + "\n", encoding="utf-8")
