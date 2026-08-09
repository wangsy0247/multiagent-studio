"""AgentCard — 团队成员能力快照。

单文件存储: 每个 project 一个 agent_card.json, 包含该项目的所有成员卡片。
存储路径: {data_root}/users/{user_id}/projects/{project_id}/agent_card.json

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
- 读取: 注入到 TeammateAgent system prompt 中
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
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
    updated_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.updated_at:
            self.updated_at = _now_iso()


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _card_file_path(project_id: str, user_id: str) -> Path:
    """返回项目的 agent_card.json 路径."""
    paths = get_paths()
    return paths.base_dir / "users" / user_id / "projects" / project_id / "agent_card.json"


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
# 读取
# ---------------------------------------------------------------------------


def get_card(project_id: str, agent_name: str, *, user_id: str) -> AgentCard | None:
    """获取单个 agent card."""
    cards = load_project_cards(project_id, user_id=user_id)
    return cards.get(agent_name)


# ---------------------------------------------------------------------------
# 缓存辅助 — 通过 mtime 判断卡片是否需要重新生成
# ---------------------------------------------------------------------------


def _get_config_mtime(agent_name: str, user_id: str) -> float:
    """返回影响 agent card 的所有配置文件中最新的 mtime.

    检测文件:
    - {base}/users/{uid}/config.yaml          (L1 全局配置, 影响 model/api_key)
    - {base}/users/{uid}/agents/{name}/config.yaml (L2 单 agent 配置)
    - {base}/users/{uid}/agents/{name}/SOUL.md     (影响 description)
    - extensions_config.json                  (MCP/skill 开关, 影响 card.tools)
    """
    paths = get_paths()
    config_files = [
        paths.base_dir / "users" / user_id / "config.yaml",
        paths.base_dir / "users" / user_id / "agents" / agent_name / "config.yaml",
        paths.base_dir / "users" / user_id / "agents" / agent_name / "SOUL.md",
    ]
    # ── MCP/工具注册表配置: 按其配置的路径解析 (支持 EXTENSIONS_CONFIG_PATH) ──
    try:
        from harness.config.extensions_config import ExtensionsConfig
        ext_path = ExtensionsConfig.resolve_config_path()
        if ext_path is not None:
            config_files.append(ext_path)
    except Exception:
        # 路径解析失败 (如 env 指向的文件缺失) 不影响主流程, 跳过即可
        pass
    max_mtime = 0.0
    for f in config_files:
        if f.exists():
            mtime = f.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
    return max_mtime


def is_card_stale(project_id: str, agent_name: str, *, user_id: str) -> bool:
    """判断 agent 的能力卡片是否需要重新生成.

    比较缓存卡片的 updated_at 与配置文件的最新 mtime:
    - 无缓存 → True (需要生成)
    - 配置文件比卡片新 → True (需要生成)
    - 卡片比配置文件新 → False (使用缓存)
    """
    card = get_card(project_id, agent_name, user_id=user_id)
    if card is None:
        return True

    config_mtime = _get_config_mtime(agent_name, user_id)
    try:
        card_time = datetime.fromisoformat(card.updated_at).timestamp()
    except (ValueError, OSError):
        return True

    return config_mtime > card_time


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

    # ── 工具: 从 tool_groups 展开 (per-agent MCP 子集过滤, 与实际装配一致) ──
    tools: list[str] = []
    if tool_registry is not None:
        _enabled_mcp = (
            effective_config.enabled_mcp_servers if effective_config is not None else {}
        )
        for group in tool_groups:
            group_tools = tool_registry.get_tools_by_category(group)
            if group == "mcp" and _enabled_mcp:
                from harness.mcp_integration.filter import filter_mcp_tools_by_agent
                group_tools = filter_mcp_tools_by_agent(group_tools, _enabled_mcp)
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
# Phase 5: spawn 自检 — AgentCard.skills 收敛到实际可用集合
# ---------------------------------------------------------------------------


def sync_agent_card_skills(
    project_id: str,
    agent_name: str,
    *,
    user_id: str,
    available_skills: Iterable[str],
) -> bool:
    """将成员的 AgentCard.skills 收敛到实际可用的技能集合.

    spawn 自检发现 skill 加载失败/白名单全过滤时调用, 剔除该成员实际
    不可用的 skills, 保证 Lead 的 <team_capabilities> 反映真实能力。
    无卡片或无变化时返回 False (未写盘)。
    """
    cards = load_project_cards(project_id, user_id=user_id)
    card = cards.get(agent_name)
    if card is None:
        return False
    available = set(available_skills)
    new_skills = [s for s in card.skills if s in available]
    if new_skills == list(card.skills):
        return False
    card.skills = new_skills
    save_project_cards(project_id, cards, user_id=user_id)
    logger.info(
        "AgentCard skills synced for '%s': kept %d/%d (project=%s)",
        agent_name, len(new_skills), len(available), project_id,
    )
    return True


# ---------------------------------------------------------------------------
# 领域匹配 — 计算 AgentCard 与任务的匹配分
# ---------------------------------------------------------------------------

def compute_card_task_match(card: AgentCard, task_title: str, task_description: str) -> float:
    """计算 AgentCard 与任务的领域匹配分.

    评分维度:
    - 工具匹配: 任务描述中提到卡片的工具 → +25/个
    - 技能匹配: 任务描述中提到卡片的技能 → +30/个
    - 关键词重叠: 卡片描述词与任务词的 Jaccard 重叠 → +2/个
    - 中文关键词兜底: 卡片描述与任务文本的 CJK bigram 重叠 → +3/个
      (中文任务描述不含英文工具名, 工具/技能匹配几乎不命中,
      靠此处避免匹配分恒为 0 退化为纯负载均衡)

    返回值 ≥ 50 表示强匹配 (≥2 个工具命中 或 1 技能+1 工具).
    """
    score = 0.0
    task_text = f"{task_title} {task_description}".lower()

    for tool in card.tools:
        if tool.lower() in task_text:
            score += 25

    for skill in card.skills:
        if skill.lower() in task_text:
            score += 30

    stop_words = {"的", "了", "在", "是", "和", "与", "或",
                  "the", "a", "an", "is", "of", "to", "in", "and"}
    card_words = set(card.description.lower().split()) - stop_words
    task_words = set(task_text.split()) - stop_words
    score += len(card_words & task_words) * 2

    # ── 中文关键词兜底: 中文无空格分词, 用 CJK bigram 重叠计分 (双向:
    # 卡片 description 中的领域词命中任务文本即计分) ──
    card_grams = _cjk_bigrams(card.description)
    task_grams = _cjk_bigrams(f"{task_title} {task_description}")
    score += len(card_grams & task_grams) * 3

    return score


_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def _cjk_bigrams(text: str) -> set[str]:
    """提取文本中连续 CJK 片段的 bigram 集合 (单字片段保留单字)."""
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(text or ""):
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return grams


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
        tools_str = ", ".join(card.tools[:10]) if card.tools else "none"
        if len(card.tools) > 10:
            tools_str += f" …(+{len(card.tools) - 10})"
        desc = card.description[:120] if card.description else "no description"
        model_short = card.model or "?"

        lines.append(
            f"{role_icon} **{card.display_name}** (`{name}`) — {card.role}\n"
            f"  Model: {model_short} | Tools: {tools_str}\n"
            f"  Description: {desc}"
        )

    return "\n".join(lines)
