"""AgentCard — 团队成员能力快照。

单文件存储: 每个 project 一个 agent_card.json, 包含该项目的所有成员卡片。
存储路径: {data_root}/users/{user_id}/project/{project_id}/agent_card.json

JSON 结构:
    {
      "project_id": "...",
      "cards": {
        "coder": { "name": "coder", "display_name": "...", ... },
        "researcher": { ... }
      },
      "updated_at": "2026-07-14T..."
    }

生命周期:
- 创建: Orchestrator.initialize() 时全量生成
- 增/删/改: 后续由项目管理 API 调用 (添加/移除成员时更新)
- 读取: 注入到 TeammateAgent system prompt 中
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# AgentCard 模型
# ---------------------------------------------------------------------------


class AgentCard(BaseModel):
    """Team member 能力快照."""

    name: str                                          # agent 名称 (用于消息路由和任务分配)
    display_name: str = ""                             # 显示名称
    description: str = ""                              # 能力描述 (来自 AgentConfig.description)
    tools: list[str] = Field(default_factory=list)     # 可用工具名列表
    skills: list[str] = Field(default_factory=list)    # 技能名列表
    model: str = ""                                    # 使用的模型
    role: str = "member"                               # "lead" | "member"
    # ── 元数据 ──
    created_at: str = ""
    updated_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _card_file_path(project_id: str, user_id: str) -> Path:
    """返回项目的 agent_card.json 路径."""
    paths = get_paths()
    return paths.base_dir / "users" / user_id / "project" / project_id / "agent_card.json"


def _ensure_dir(file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 单文件读写
# ---------------------------------------------------------------------------


def load_project_cards(project_id: str, *, user_id: str) -> dict[str, AgentCard]:
    """加载项目的所有 agent cards. 返回 {agent_name: AgentCard}."""
    file_path = _card_file_path(project_id, user_id)
    if not file_path.exists():
        return {}

    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        cards: dict[str, AgentCard] = {}
        raw_cards: dict = data.get("cards", {})
        for name, card_data in raw_cards.items():
            try:
                cards[name] = AgentCard(**card_data)
            except Exception as exc:
                logger.warning("Failed to parse AgentCard '%s': %s", name, exc)
        return cards
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load agent cards from %s: %s", file_path, exc)
        return {}


def save_project_cards(project_id: str, cards: dict[str, AgentCard], *, user_id: str) -> None:
    """全量保存项目的所有 agent cards."""
    file_path = _card_file_path(project_id, user_id)
    _ensure_dir(file_path)

    raw_cards: dict[str, dict] = {}
    for name, card in cards.items():
        card.updated_at = _now_iso()
        raw_cards[name] = card.model_dump()

    data = {
        "project_id": project_id,
        "cards": raw_cards,
        "updated_at": _now_iso(),
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.debug("Agent cards saved: project=%s count=%d", project_id, len(cards))


# ---------------------------------------------------------------------------
# CRUD 操作
# ---------------------------------------------------------------------------


def add_card(project_id: str, card: AgentCard, *, user_id: str) -> None:
    """添加一个 agent card 到项目 (已存在则覆盖)."""
    cards = load_project_cards(project_id, user_id=user_id)
    card.updated_at = _now_iso()
    if not card.created_at:
        card.created_at = _now_iso()
    cards[card.name] = card
    save_project_cards(project_id, cards, user_id=user_id)
    logger.info("AgentCard added: project=%s agent=%s", project_id, card.name)


def remove_card(project_id: str, agent_name: str, *, user_id: str) -> bool:
    """从项目中删除一个 agent card. 返回是否成功删除."""
    cards = load_project_cards(project_id, user_id=user_id)
    if agent_name not in cards:
        return False
    del cards[agent_name]
    save_project_cards(project_id, cards, user_id=user_id)
    logger.info("AgentCard removed: project=%s agent=%s", project_id, agent_name)
    return True


def update_card(project_id: str, card: AgentCard, *, user_id: str) -> bool:
    """更新项目中的一个 agent card. 返回是否成功 (card 不存在则返回 False)."""
    cards = load_project_cards(project_id, user_id=user_id)
    if card.name not in cards:
        return False
    card.updated_at = _now_iso()
    cards[card.name] = card
    save_project_cards(project_id, cards, user_id=user_id)
    logger.info("AgentCard updated: project=%s agent=%s", project_id, card.name)
    return True


def get_card(project_id: str, agent_name: str, *, user_id: str) -> AgentCard | None:
    """获取单个 agent card."""
    cards = load_project_cards(project_id, user_id=user_id)
    return cards.get(agent_name)


def delete_project_cards(project_id: str, *, user_id: str) -> bool:
    """删除项目的 agent_card.json 文件. 返回是否成功."""
    file_path = _card_file_path(project_id, user_id)
    if not file_path.exists():
        return False
    file_path.unlink()
    # 尝试删除空目录
    try:
        file_path.parent.rmdir()
    except OSError:
        pass
    logger.info("Agent cards deleted: project=%s", project_id)
    return True


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def generate_agent_card(
    agent_name: str,
    *,
    user_id: str,
    tool_registry: Any = None,
    skill_storage: Any = None,
    effective_config: Any = None,
    role: str = "member",
) -> AgentCard:
    """从 AgentConfig + ToolRegistry + SkillStorage 生成 AgentCard.

    Args:
        agent_name: Agent 名称
        user_id: 用户 ID
        tool_registry: ToolRegistry 实例 (用于展开 tool_groups → 工具名列表)
        skill_storage: SkillStorage 实例
        effective_config: EffectiveConfig (优先于 load_agent_config)
        role: 在 team 中的角色

    Returns:
        AgentCard: 能力快照
    """
    display_name = agent_name
    description = ""
    skills: list[str] = []
    model = ""

    if effective_config is not None:
        display_name = effective_config.agent_display_name or agent_name
        description = effective_config.agent_description or ""
        skills = list(effective_config.skills) if effective_config.skills else []
        model = effective_config.model or ""
        tool_groups = list(effective_config.tool_groups) if effective_config.tool_groups else []
    else:
        # Fallback: 从旧 AgentConfig 加载
        from harness.config.agents_config import load_agent_config
        cfg = load_agent_config(agent_name, user_id=user_id)
        if cfg is not None:
            display_name = cfg.display_name or agent_name
            description = cfg.description or ""
            skills = list(cfg.skills)
            model = cfg.model or ""
            tool_groups = list(cfg.tool_groups)
        else:
            tool_groups = []

    # ── 工具: 从 tool_groups 展开 ──
    tools: list[str] = []
    if tool_registry is not None:
        for group in tool_groups:
            group_tools = tool_registry.get_tools_by_category(group)
            for t in group_tools:
                name = getattr(t, "name", str(t))
                if name not in tools:
                    tools.append(name)

    # ── 从 SOUL 补充描述 ──
    if not description:
        from harness.config.agents_config import load_agent_soul
        soul = load_agent_soul(agent_name, user_id=user_id)
        if soul:
            description = soul[:200].replace("\n", " ").strip()

    return AgentCard(
        name=agent_name,
        display_name=display_name,
        description=description,
        tools=tools,
        skills=skills,
        model=model,
        role=role,
    )


# ---------------------------------------------------------------------------
# 格式化 — 注入 system prompt
# ---------------------------------------------------------------------------


def format_cards_for_prompt(cards: dict[str, AgentCard]) -> str:
    """将 agent cards 格式化为 system prompt 中可注入的紧凑文本.

    用于 TeammateAgent._build_system_prompt(), 让所有成员了解团队能力.
    """
    if not cards:
        return ""

    lines: list[str] = []
    for name, card in cards.items():
        role_icon = "⭐" if card.role == "lead" else "👤"
        tools_str = ", ".join(card.tools[:10]) if card.tools else "无"
        if len(card.tools) > 10:
            tools_str += f" …(+{len(card.tools) - 10})"
        desc = card.description[:120] if card.description else "无描述"
        model_short = card.model or "?"

        lines.append(
            f"{role_icon} **{card.display_name}** (`{name}`) — {card.role}\n"
            f"  模型: {model_short} | 工具: {tools_str}\n"
            f"  描述: {desc}"
        )

    return "\n".join(lines)
