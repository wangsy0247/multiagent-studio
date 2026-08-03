"""Agent configuration management — SOUL.md, config.yaml for custom agents.

Custom agents are stored per-user under ``{data_root}/users/{user_id}/agents/{name}/``.
Layout:
    {data_root}/users/{user_id}/agents/{name}/
    ├── SOUL.md                ← personality / behavioral definition
    ├── config.yaml            ← full runtime config (model, tools, memory, features, ...)
    └── extensions_config.yaml ← MCP server enable/disable subset
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
AGENT_EXTENSIONS_FILENAME = "extensions_config.yaml"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_AGENT_NAME = "default"


def validate_agent_name(name: str) -> str:
    if not name:
        raise ValueError("Agent 名称不能为空")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Agent 名称 '{name}' 不合法。"
            f" 名称只能包含英文字母、数字、下划线 (_) 和短横线 (-)，"
            f" 不能包含空格或特殊字符。"
            f" 建议将空格替换为短横线，例如 'Ai-Engineer'。"
        )
    return name


def is_default_agent(name: str) -> bool:
    return name == DEFAULT_AGENT_NAME


# ---------------------------------------------------------------------------
# 子模型
# ---------------------------------------------------------------------------

class AgentMemoryFields(BaseModel):
    """Per-Agent 记忆配置 (L2)."""
    max_facts: int = 10
    injection_enabled: bool = True
    max_injection_tokens: int = 500


class AgentFeaturesFields(BaseModel):
    """Per-Agent 功能开关 (L2)."""
    summarization: bool = True
    subagent: bool = True
    langfuse: bool = True
    guardrail: bool = False


class AgentLimitsFields(BaseModel):
    """Per-Agent 限制 (L2)."""
    max_turns: int = 50
    timeout_seconds: int = 900


class AgentTeamFields(BaseModel):
    """Per-Agent Team 配置 (L2)."""
    can_delegate: bool = True
    memory_scope: str = "agent"
    # 成员工作区隔离 (Phase 6): worktree=独立 git worktree, 与其他成员隔离;
    # shared=共享 thread 工作区 (默认, 协作产物互见)
    isolation: str = "shared"


class AgentSubagentsFields(BaseModel):
    """Per-Agent SubAgents 配置 (L2)."""
    timeout_seconds: int = 900
    max_concurrent: int = 3


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Configuration for a custom agent — 完整运行时配置."""

    # 标识
    name: str
    display_name: str = ""
    description: str = ""

    # 模型 (必选 — 创建 agent 时必须指定)
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096

    # 工具 (扩展到系统默认)
    tool_groups: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    # 记忆
    memory: AgentMemoryFields = Field(default_factory=AgentMemoryFields)

    # 功能开关
    features: AgentFeaturesFields = Field(default_factory=AgentFeaturesFields)

    # 限制
    limits: AgentLimitsFields = Field(default_factory=AgentLimitsFields)

    # Team
    team: AgentTeamFields = Field(default_factory=AgentTeamFields)

    # SubAgents
    subagents: AgentSubagentsFields = Field(default_factory=AgentSubagentsFields)

    # 元数据
    created_at: str = ""
    updated_at: str = ""

    # ════════════════════════════════════════════════════════════════════
    # 向后兼容属性 (旧字段 → 新子模型映射)
    # ════════════════════════════════════════════════════════════════════

    @property
    def can_delegate(self) -> bool:
        return self.team.can_delegate

    @can_delegate.setter
    def can_delegate(self, value: bool) -> None:
        self.team.can_delegate = value

    @property
    def memory_scope(self) -> str:
        return self.team.memory_scope

    @memory_scope.setter
    def memory_scope(self, value: str) -> None:
        self.team.memory_scope = value

    @property
    def max_turns(self) -> int:
        return self.limits.max_turns

    @max_turns.setter
    def max_turns(self, value: int) -> None:
        self.limits.max_turns = value

    @property
    def timeout_seconds(self) -> int:
        return self.limits.timeout_seconds

    @timeout_seconds.setter
    def timeout_seconds(self, value: int) -> None:
        self.limits.timeout_seconds = value


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


def _migrate_flat_to_nested(data: dict[str, Any]) -> dict[str, Any]:
    """将旧版平铺格式的 agent config 迁移到新的嵌套子模型格式.

    旧格式 (flat):
        model: gpt-4o
        can_be_lead: true   # 已废弃, 迁移时静默丢弃
        memory_scope: agent
        max_turns: 50
        timeout_seconds: 900

    新格式 (nested):
        model: gpt-4o
        memory: { backend: file, ... }
        team: { can_delegate: true, memory_scope: agent, ... }
        limits: { max_turns: 50, ... }
    """
    # 如果已经是新格式 (包含嵌套 key), 直接返回
    if any(k in data for k in ("memory", "features", "limits", "team", "subagents")):
        return data

    # 迁移平铺字段到子模型
    if "memory" not in data:
        data["memory"] = {}
    if "features" not in data:
        data["features"] = {}
    if "limits" not in data:
        data["limits"] = {
            "max_turns": data.pop("max_turns", 50),
            "timeout_seconds": data.pop("timeout_seconds", 900),
        }
    if "team" not in data:
        data["team"] = {
            "can_delegate": data.pop("can_delegate", True),
            "memory_scope": data.pop("memory_scope", "agent"),
        }
    if "subagents" not in data:
        data["subagents"] = {}

    # 清理可能残留的旧平铺字段 (can_be_lead 已废弃, 仅保留用于旧数据迁移)
    for old_key in ("can_be_lead", "can_delegate", "memory_scope", "max_turns",
                    "timeout_seconds", "isolation", "vision"):
        data.pop(old_key, None)

    return data


def load_agent_config(name: str, *, user_id: str | None = None) -> AgentConfig | None:
    """Load an agent's config.yaml (自动迁移旧格式)."""
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
    data = _migrate_flat_to_nested(data)

    # ── 规范化: YAML 空值 → 空字符串, datetime → ISO 字符串 ──
    str_fields = ("name", "display_name", "description", "model", "created_at", "updated_at")
    for field in str_fields:
        val = data.get(field)
        if val is None:
            data[field] = ""
        elif isinstance(val, datetime):
            data[field] = val.isoformat()

    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}
    return AgentConfig(**data)


def save_agent_config(name: str, cfg: AgentConfig, *, user_id: str | None = None) -> None:
    """Save an agent's config.yaml (层级化 YAML 输出)."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    cfg.updated_at = datetime.now(UTC).isoformat()
    if not cfg.created_at:
        cfg.created_at = cfg.updated_at

    content = _format_agent_config_yaml(cfg)
    config_file = agent_dir / AGENT_CONFIG_FILENAME
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(content)


def _format_agent_config_yaml(cfg: AgentConfig) -> str:
    """将 AgentConfig 格式化为层级化 YAML 字符串."""
    import yaml as _yaml

    def _dict_block(d: dict, indent: int = 2) -> str:
        """Render a flat dict as indented YAML lines."""
        if not d:
            return " {}"
        lines = []
        for k, v in d.items():
            if isinstance(v, bool):
                lines.append(f"{' ' * indent}{k}: {str(v).lower()}")
            elif isinstance(v, str):
                lines.append(f"{' ' * indent}{k}: {v}")
            else:
                lines.append(f"{' ' * indent}{k}: {v}")
        return "\n" + "\n".join(lines)

    sections: list[str] = []

    # ── 标识 ──
    sections.append(f"# ── Agent 标识 ──")
    sections.append(f"name: {cfg.name}")
    sections.append(f'display_name: "{cfg.display_name or cfg.name}"')
    desc = cfg.description or ""
    sections.append(f'description: "{desc}"')
    sections.append("")

    # ── 模型 ──
    sections.append(f"# ── 模型 ──")
    sections.append(f"model: {cfg.model}")
    sections.append(f"temperature: {cfg.temperature}")
    sections.append(f"max_tokens: {cfg.max_tokens}")
    sections.append("")

    # ── 工具 ──
    sections.append(f"# ── 工具 (扩展到 L0 系统默认) ──")
    sections.append(_yaml.dump({"tool_groups": cfg.tool_groups}, default_flow_style=False, allow_unicode=True).strip())
    sections.append(_yaml.dump({"skills": cfg.skills}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── 记忆 ──
    sections.append(f"# ── 记忆 ──")
    mem = cfg.memory.model_dump()
    sections.append(_yaml.dump({"memory": mem}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── 功能开关 ──
    sections.append(f"# ── 功能开关 ──")
    feat = cfg.features.model_dump()
    sections.append(_yaml.dump({"features": feat}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── 限制 ──
    sections.append(f"# ── 限制 ──")
    lim = cfg.limits.model_dump()
    sections.append(_yaml.dump({"limits": lim}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── Team ──
    sections.append(f"# ── Team ──")
    sections.append(f"# isolation: worktree=该成员在独立 git worktree 工作, 产物与其他成员隔离;")
    sections.append(f"#            shared=共享 thread 工作区 (默认, 协作产物互见)")
    team = cfg.team.model_dump()
    sections.append(_yaml.dump({"team": team}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── SubAgents ──
    sections.append(f"# ── SubAgents ──")
    sub = cfg.subagents.model_dump()
    sections.append(_yaml.dump({"subagents": sub}, default_flow_style=False, allow_unicode=True).strip())
    sections.append("")

    # ── 元数据 ──
    sections.append(f"# ── 元数据 ──")
    sections.append(f'created_at: "{cfg.created_at}"')
    sections.append(f'updated_at: "{cfg.updated_at}"')
    sections.append("")

    return "\n".join(sections)


def create_default_agent(user_id: str) -> AgentConfig:
    """创建 default agent — 使用系统默认模型和完整配置.

    如果 default agent 已存在, 直接返回已有配置.
    """
    existing = load_agent_config(DEFAULT_AGENT_NAME, user_id=user_id)
    if existing is not None:
        return existing

    # 从环境变量读取默认模型, 无配置则用 gpt-4o
    import os as _os
    default_model = _os.getenv("DEFAULT_MODEL", "gpt-4o")
    cfg = AgentConfig(
        name=DEFAULT_AGENT_NAME,
        display_name="Default Agent",
        description="系统默认 Agent — 用于单 Agent 模式和 Team Lead。",
        model=default_model,
    )
    save_agent_config(DEFAULT_AGENT_NAME, cfg, user_id=user_id)
    save_agent_soul(
        DEFAULT_AGENT_NAME,
        "# Default Agent — Multi-Agent Orchestrator\n\n"
        "You are an intelligent AI assistant. "
        "Your role is to understand user requests deeply, "
        "decide on the best approach, and deliver high-quality results.\n\n",
        user_id=user_id,
    )
    _save_agent_extensions_template(DEFAULT_AGENT_NAME, user_id)
    logger.info("Created default agent for user '%s'", user_id)
    return cfg


def save_agent_extensions(
    name: str, mcp_servers: dict[str, bool], *, user_id: str | None = None,
    skills: dict[str, bool] | None = None,
) -> None:
    """保存 per-agent extensions_config.yaml."""
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {"mcp_servers": mcp_servers}
    if skills:
        data["skills"] = skills

    ext_path = agent_dir / AGENT_EXTENSIONS_FILENAME
    with open(ext_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)


def _save_agent_extensions_template(name: str, user_id: str) -> None:
    """创建 agent 时生成 extensions_config.yaml 模板."""
    save_agent_extensions(
        name,
        mcp_servers={"github": False, "filesystem": False, "brave-search": False},
        user_id=user_id,
    )


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
