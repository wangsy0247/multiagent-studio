"""Project index — read-only access to user project metadata.

供单 agent 项目感知使用 (Team 模式重构 Phase 4, 决策: 单 agent 对项目只读)。
本模块所有函数严格只读磁盘: 不写入、不迁移、不创建目录
(与 team 模式 ``orchestrator._load_project_json`` 的旧格式自动迁移不同,
这里旧格式仅读取, 绝不落盘)。

数据布局::

    {base_dir}/users/{uid}/projects/{pid}/project.json   (新格式)
    {base_dir}/users/{uid}/projects/{pid}.json           (旧格式, 仅读取)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)

# 单行描述的最大长度 (超出截断)
_MAX_DESC_CHARS = 80


def _projects_dir(user_id: str) -> Path:
    return get_paths().base_dir / "users" / user_id / "projects"


# project_id 可能来自 LLM 工具入参 — 白名单校验防路径穿越
_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_project_id(project_id: str) -> str:
    if not project_id or not _SAFE_PROJECT_ID_RE.match(project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    return project_id


def _read_json(path: Path) -> dict[str, Any] | None:
    """读取 JSON dict; 失败静默降级 (log warning, 返回 None)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read project file %s: %s", path, exc)
        return None


def load_project(project_id: str, user_id: str) -> dict[str, Any] | None:
    """只读加载单个项目的 project.json (新格式优先; 旧格式仅读取不迁移)."""
    _validate_project_id(project_id)
    base = _projects_dir(user_id)
    for path in (base / project_id / "project.json", base / f"{project_id}.json"):
        if path.is_file():
            data = _read_json(path)
            if data is not None:
                data.setdefault("id", project_id)
            return data
    return None


def list_projects(user_id: str) -> list[dict[str, Any]]:
    """枚举用户的全部项目 (只读). 每个条目含 id/name/description/members."""
    base = _projects_dir(user_id)
    if not base.is_dir():
        return []

    projects: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 新格式: projects/{pid}/project.json
    for path in sorted(base.glob("*/project.json")):
        data = _read_json(path)
        if data is None:
            continue
        pid = data.get("id") or path.parent.name
        data.setdefault("id", pid)
        projects.append(data)
        seen.add(pid)

    # 旧格式: projects/{pid}.json (仅读取, 不做迁移写入)
    for path in sorted(base.glob("*.json")):
        pid = path.stem
        if pid in seen:
            continue
        data = _read_json(path)
        if data is None:
            continue
        data.setdefault("id", pid)
        projects.append(data)

    return projects


def _format_member_names(members: Any) -> str:
    """members 兼容两种形态: ["agent_a", ...] 或 [{"name": ...}, ...]."""
    if not isinstance(members, list) or not members:
        return "none"
    names: list[str] = []
    for m in members:
        if isinstance(m, dict):
            names.append(str(m.get("name") or m.get("agent") or "?"))
        else:
            names.append(str(m))
    return ", ".join(names)


def format_projects_index(
    projects: list[dict[str, Any]], *, max_entries: int = 20
) -> str:
    """格式化为 ``<projects>`` 索引块 (每项目一行); 无项目时返回空串."""
    if not projects:
        return ""

    total = len(projects)
    shown = projects[:max(0, max_entries)]
    lines: list[str] = []
    for p in shown:
        pid = p.get("id", "?")
        name = p.get("name") or pid
        desc = (p.get("description") or "").strip().replace("\n", " ")
        if len(desc) > _MAX_DESC_CHARS:
            desc = desc[:_MAX_DESC_CHARS] + "…"
        line = f"- {name} (id: {pid})"
        if desc:
            line += f" — {desc}"
        line += f" | members: {_format_member_names(p.get('members'))}"
        lines.append(line)

    if total > len(shown):
        lines.append(
            f"({total} projects in total, showing the first {len(shown)}; "
            f"use the project_info tool to view a specific project)"
        )

    return "<projects>\n" + "\n".join(lines) + "\n</projects>"
