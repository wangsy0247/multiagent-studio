"""项目 + 任务面板 API."""

import json as _json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import resolve_fs_user_id
from app.db.engine import get_db
from harness.config.paths import get_paths

logger = logging.getLogger(__name__)
router = APIRouter(tags=["项目管理"])


# ── 路径工具 ──

def _proj_dir(user_id: str) -> Path:
    """用户项目根目录: {base}/users/{uid}/projects/."""
    return get_paths().base_dir / "users" / user_id / "projects"


def _project_file(project_id: str, user_id: str) -> Path:
    """单个项目的元数据文件: {base}/users/{uid}/projects/{pid}/project.json.

    向后兼容旧格式 projects/{pid}.json: 若存在则自动迁移到新格式.
    """
    new_path = _proj_dir(user_id) / project_id / "project.json"
    old_path = _proj_dir(user_id) / f"{project_id}.json"

    if new_path.exists():
        return new_path

    # 旧格式自动迁移
    if old_path.exists():
        logger.info("Migrating project '%s' from old format → new format", project_id)
        old_data = _json.loads(old_path.read_text())
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(_json.dumps(old_data, indent=2))
        old_path.unlink()
        return new_path

    return new_path  # 新项目, 尚未创建


def _project_dir(project_id: str, user_id: str) -> Path:
    """单个项目目录: {base}/users/{uid}/projects/{pid}/."""
    return _proj_dir(user_id) / project_id


def _tasks_path(project_id: str, user_id: str) -> Path:
    """项目任务文件: {base}/users/{uid}/projects/{pid}/tasks.json."""
    d = _project_dir(project_id, user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"


def _load_tasks(project_id: str, user_id: str) -> list:
    p = _tasks_path(project_id, user_id)
    if not p.exists():
        return []
    return _json.loads(p.read_text())


def _save_tasks(project_id: str, tasks: list, user_id: str):
    _tasks_path(project_id, user_id).write_text(_json.dumps(tasks, indent=2))


# ── 路由 ──

@router.get("")
async def list_projects(
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    d = _proj_dir(uid)
    if not d.exists():
        return {"projects": [], "count": 0}
    projects = []
    seen: set[str] = set()
    # 新格式: projects/{pid}/project.json
    for f in sorted(d.glob("*/project.json")):
        try:
            proj = _json.loads(f.read_text())
            if proj.get("id") not in seen:
                projects.append(proj)
                seen.add(proj.get("id", ""))
        except Exception:
            pass
    # 旧格式兼容: projects/{pid}.json (扁平文件)
    for f in sorted(d.glob("*.json")):
        try:
            proj = _json.loads(f.read_text())
            if proj.get("id") not in seen:
                projects.append(proj)
                seen.add(proj.get("id", ""))
        except Exception:
            pass
    return {"projects": projects, "count": len(projects)}


@router.post("")
async def create_project(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = await resolve_fs_user_id(body.get("user_id"), auth_header, db)
    d = _proj_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    pid = body.get("id", str(uuid.uuid4())[:8])
    now = datetime.now().isoformat()
    project = {
        "id": pid,
        "name": body.get("name", "New Project"),
        "description": body.get("description", ""),
        "members": body.get("members", []),
        "thread_count": 0,
        "task_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    p = _project_file(pid, user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    p = _project_file(project_id, uid)
    if not p.exists():
        raise HTTPException(404, "Project not found")
    return _json.loads(p.read_text())


@router.put("/{project_id}")
async def update_project(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = await resolve_fs_user_id(body.get("user_id"), auth_header, db)
    p = _project_file(project_id, user_id)
    if not p.exists():
        raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    for k in ("name", "description", "members"):
        if k in body:
            project[k] = body[k]
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    # 删除新格式目录 (如果存在)
    d = _project_dir(project_id, uid)
    if d.exists():
        shutil.rmtree(d)
    # 兼容旧格式文件
    old = _proj_dir(uid) / f"{project_id}.json"
    if old.exists():
        old.unlink()
    if not d.exists() and not old.exists():
        raise HTTPException(404, "Project not found")
    return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/members")
async def add_member(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = await resolve_fs_user_id(body.get("user_id"), auth_header, db)
    agent_name = body.get("agent_name", "")
    p = _project_file(project_id, user_id)
    if not p.exists():
        raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    members = project.get("members", [])
    if agent_name not in members:
        members.append(agent_name)
    project["members"] = members
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.delete("/{project_id}/members/{agent_name}")
async def remove_member(
    project_id: str,
    agent_name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    p = _project_file(project_id, uid)
    if not p.exists():
        raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    project["members"] = [m for m in project.get("members", []) if m != agent_name]
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.get("/{project_id}/tasks")
async def list_tasks(
    project_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    return {"tasks": _load_tasks(project_id, uid)}


@router.post("/{project_id}/tasks")
async def create_task(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = await resolve_fs_user_id(body.get("user_id"), auth_header, db)
    tasks = _load_tasks(project_id, user_id)
    # 校验 status (与后端 TeamTaskStatus 5 态保持一致)
    valid_statuses = {"pending", "in_progress", "completed", "failed", "cancelled"}
    raw_status = body.get("status", "pending")
    if raw_status not in valid_statuses:
        raise HTTPException(400, f"Invalid status: {raw_status}. Must be one of {valid_statuses}")

    task = {
        "id": str(uuid.uuid4())[:8],
        "project_id": project_id,
        "title": body.get("title", ""),
        "description": body.get("description", ""),
        "status": raw_status,
        "assigned_agent": body.get("assigned_agent"),
        "priority": body.get("priority", "medium"),
        "origin": "user",  # 用户手工创建 — 团队运行时不会被当作遗留任务清理
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    tasks.append(task)
    _save_tasks(project_id, tasks, user_id)
    return task


@router.put("/{project_id}/tasks/{task_id}")
async def update_task(project_id: str, task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = await resolve_fs_user_id(body.get("user_id"), auth_header, db)
    tasks = _load_tasks(project_id, user_id)
    for t in tasks:
        if t["id"] == task_id:
            for k in ("title", "description", "status", "assigned_agent", "priority"):
                if k in body:
                    # 校验 status 合法性
                    if k == "status":
                        valid_statuses = {"pending", "in_progress", "completed", "failed", "cancelled"}
                        if body[k] not in valid_statuses:
                            raise HTTPException(400, f"Invalid status: {body[k]}. Must be one of {valid_statuses}")
                    t[k] = body[k]
            t["updated_at"] = datetime.now().isoformat()
            _save_tasks(project_id, tasks, user_id)
            return t
    raise HTTPException(404, "Task not found")


# ── Agent Cards ──

@router.get("/{project_id}/agent-cards")
async def get_agent_cards(
    project_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    """获取项目的 agent-card.json 内容 (成员能力快照)."""
    uid = await resolve_fs_user_id(user_id, authorization, db)
    try:
        from harness.team.agent_card import load_project_cards
        cards = load_project_cards(project_id, user_id=uid)
        return {
            "project_id": project_id,
            "cards": {name: card.model_dump() for name, card in cards.items()},
            "count": len(cards),
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to load agent cards: {exc}")


@router.delete("/{project_id}/tasks/{task_id}")
async def delete_task(
    project_id: str,
    task_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
    db: AsyncSession = Depends(get_db),
):
    uid = await resolve_fs_user_id(user_id, authorization, db)
    tasks = _load_tasks(project_id, uid)
    tasks = [t for t in tasks if t["id"] != task_id]
    _save_tasks(project_id, tasks, uid)
    return {"status": "deleted", "id": task_id}
