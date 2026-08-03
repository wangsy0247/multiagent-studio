"""Built-in read-only project awareness tools for single-agent mode.

Team 模式重构 Phase 4 (决策: 单 agent 对项目只读):
- ``project_info``         — 项目元数据 + 成员 AgentCard 摘要 + 团队记忆摘录
- ``project_memory_search`` — 检索项目任务记忆 (TaskMemoryStore.find_related)

硬约束: 两个工具严格只读磁盘, 不写任何文件, 避免与 team run 并发写冲突。
注意: 不直接实例化 TeamMemoryStore / TaskMemoryStore 读取, 因为它们构造时
会 mkdir; 仅当目标目录已存在时才使用 TaskMemoryStore (此时 mkdir 为 no-op)。
user_id 取自 InjectedState (与 session_search 工具同一模式)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from harness.config.paths import get_paths
from harness.memory.project_index import load_project

logger = logging.getLogger(__name__)

# 团队记忆 practices/pitfalls 各取前 N 条
_MAX_TEAM_MEMORY_ENTRIES = 10
# 任务记忆检索条数上限
_MAX_TASK_MEMORIES = 10


def _extract_user_id(state: dict | None) -> str:
    """从注入的 graph state 提取用户身份 (与 session_search 同一模式)."""
    return (state or {}).get("user_id") or ""


def _project_dir(project_id: str, user_id: str) -> Path:
    return get_paths().base_dir / "users" / user_id / "projects" / project_id


def _load_agent_cards_summary(project_id: str, user_id: str) -> list[str]:
    """读取 agent_card.json, 返回成员摘要行 (只读; 失败返回空列表)."""
    path = _project_dir(project_id, user_id) / "agent_card.json"
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read agent cards %s: %s", path, exc)
        return []

    lines: list[str] = []
    raw_cards: dict = data.get("cards", {}) if isinstance(data, dict) else {}
    for name, card in raw_cards.items():
        if not isinstance(card, dict):
            continue
        display = card.get("display_name") or name
        role = card.get("role") or "member"
        desc = (card.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 100:
            desc = desc[:100] + "…"
        line = f"- {display} ({name}) — {role}"
        if desc:
            line += f": {desc}"
        lines.append(line)
    return lines


def _load_team_memory_excerpt(
    project_id: str, user_id: str
) -> tuple[list[str], list[str]]:
    """读取 team_memory.json 的 practices/pitfalls 各前 N 条 (只读; 失败返回空)."""
    path = _project_dir(project_id, user_id) / "memory" / "team_memory.json"
    if not path.is_file():
        return [], []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read team memory %s: %s", path, exc)
        return [], []

    def _texts(entries: Any, key: str) -> list[str]:
        if not isinstance(entries, list):
            return []
        out: list[str] = []
        for e in entries[:_MAX_TEAM_MEMORY_ENTRIES]:
            if isinstance(e, dict):
                text = str(e.get(key) or "").strip()
            else:
                text = str(e).strip()
            if text:
                out.append(text)
        return out

    practices = _texts(data.get("best_practices"), "practice")
    pitfalls = _texts(data.get("known_pitfalls"), "pitfall")
    return practices, pitfalls


@tool
async def project_info(
    project_id: str,
    state: Annotated[dict, InjectedState] = None,  # graph state (user_id)
) -> str:
    """Read a project's metadata, member roster, and team memory (READ-ONLY).

    Returns the project's name/description/members from project.json, an
    AgentCard summary per member (name, role, capability description), and
    the top team-memory best practices / known pitfalls (up to 10 each).

    Use this tool when you need details about one of the projects listed in
    the <projects> index — e.g. who is on the team, what the project is for,
    or what lessons the team has accumulated. This tool never writes anything.

    Args:
        project_id: The project ID, as listed in the <projects> index.
    """
    user_id = _extract_user_id(state)
    if not user_id:
        return "Error: unable to determine the current user."

    project = load_project(project_id, user_id)
    if project is None:
        return (
            f"Project '{project_id}' not found. "
            "Use one of the project IDs listed in the <projects> index."
        )

    lines: list[str] = [f"# Project: {project.get('name') or project_id} (id: {project_id})"]

    desc = (project.get("description") or "").strip()
    if desc:
        lines.append(f"\nDescription: {desc}")

    members = project.get("members") or []
    if isinstance(members, list) and members:
        member_names = [
            str(m.get("name") or m.get("agent") or "?") if isinstance(m, dict) else str(m)
            for m in members
        ]
        lines.append(f"\nMembers: {', '.join(member_names)}")

    stats: list[str] = []
    if project.get("thread_count") is not None:
        stats.append(f"threads={project.get('thread_count')}")
    if project.get("task_count") is not None:
        stats.append(f"tasks={project.get('task_count')}")
    if stats:
        lines.append(f"Stats: {', '.join(stats)}")

    # ── 成员 AgentCard 摘要 ──
    card_lines = _load_agent_cards_summary(project_id, user_id)
    if card_lines:
        lines.append("\n## Member cards")
        lines.extend(card_lines)

    # ── 团队记忆摘录 (practices/pitfalls 各前 10 条) ──
    practices, pitfalls = _load_team_memory_excerpt(project_id, user_id)
    if practices:
        lines.append("\n## Team best practices")
        lines.extend(f"- {p}" for p in practices)
    if pitfalls:
        lines.append("\n## Known pitfalls")
        lines.extend(f"- {p}" for p in pitfalls)

    return "\n".join(lines)


@tool
async def project_memory_search(
    project_id: str,
    query: str,
    state: Annotated[dict, InjectedState] = None,  # graph state (user_id)
) -> str:
    """Search a project's task memories by keyword relevance (READ-ONLY).

    Task memories are structured experience records (summary, decisions,
    pitfalls, discoveries, tags) archived by the team when tasks complete.
    Memories are strictly scoped to the given project — results never leak
    across projects. Returns up to 10 matching memories. This tool never
    writes anything.

    Use short keywords rather than full sentences for the query; retry with
    synonyms when nothing matches.

    Args:
        project_id: The project ID, as listed in the <projects> index.
        query: Keywords describing what you are looking for.
    """
    user_id = _extract_user_id(state)
    if not user_id:
        return "Error: unable to determine the current user."

    project = load_project(project_id, user_id)
    if project is None:
        return (
            f"Project '{project_id}' not found. "
            "Use one of the project IDs listed in the <projects> index."
        )

    # ── 只读守卫: tasks 目录不存在时直接返回空结果,
    #    避免 TaskMemoryStore 构造时的 mkdir 产生写副作用 ──
    tasks_dir = _project_dir(project_id, user_id) / "memory" / "tasks"
    if not tasks_dir.is_dir():
        return f"No task memories found for project '{project_id}'."

    from harness.memory.task_memory import TaskMemoryStore

    # 目录已存在, 构造时的 mkdir(exist_ok=True) 为 no-op
    store = TaskMemoryStore(project_id, user_id)
    try:
        memories = await store.find_related(query, "", max_results=_MAX_TASK_MEMORIES)
    except Exception as exc:
        logger.warning("project_memory_search failed: %s", exc)
        return f"Failed to search task memories for project '{project_id}': {exc}"

    if not memories:
        return (
            f"No task memories in project '{project_id}' matched '{query}'. "
            "Try shorter keywords or synonyms."
        )

    blocks: list[str] = []
    for m in memories:
        header = f"## {m.task_title} [{m.status or '?'}]"
        if m.assigned_agent:
            header += f" (by {m.assigned_agent})"
        parts: list[str] = [header]
        if m.tags:
            parts.append(f"tags: {', '.join(m.tags)}")
        if m.summary:
            parts.append(m.summary)
        for label, entries in (
            ("decisions", m.decisions),
            ("pitfalls", m.pitfalls),
            ("discoveries", m.discoveries),
        ):
            for e in entries[:3]:
                parts.append(f"- {label}: {e}")
        blocks.append("\n".join(parts))

    return "\n\n---\n\n".join(blocks)
