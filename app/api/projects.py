"""项目 + 任务面板 API."""

import json as _json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)
router = APIRouter(tags=["项目管理"])


# ── JWT 兜底：从 Authorization header 提取真实 user_id ──

def _extract_jwt_sub(authorization: str | None) -> str | None:
    """从 Authorization: Bearer <token> 的 JWT 中提取 user_id (sub 字段)。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        from jose import jwt, JWTError
        from app.config import get_settings
        token = authorization[7:]
        settings = get_settings()
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload.get("sub")
    except (JWTError, Exception):
        return None


def _resolve_user_id(explicit: str | None, authorization: str | None = None) -> str:
    """解析真实 user_id：explicit 非 default → 直接使用；否则从 JWT 提取；兜底 default。"""
    if explicit and explicit != "default":
        return explicit
    jwt_uid = _extract_jwt_sub(authorization)
    if jwt_uid:
        logger.info(f"[_resolve_user_id] JWT 兜底 → user_id={jwt_uid}")
        return jwt_uid
    return "default"


# ── 路径工具 ──

def _proj_dir(user_id: str) -> Path:
    return get_paths().base_dir / "users" / user_id / "projects"


def _tasks_path(project_id: str, user_id: str) -> Path:
    d = get_paths().base_dir / "users" / user_id / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project_id}.json"


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
):
    uid = _resolve_user_id(user_id, authorization)
    d = _proj_dir(uid)
    if not d.exists():
        return {"projects": [], "count": 0}
    projects = []
    for f in sorted(d.glob("*.json")):
        try:
            projects.append(_json.loads(f.read_text()))
        except Exception:
            pass
    return {"projects": projects, "count": len(projects)}


@router.post("")
async def create_project(request: Request):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
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
    (d / f"{pid}.json").write_text(_json.dumps(project, indent=2))
    return project


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    uid = _resolve_user_id(user_id, authorization)
    p = _proj_dir(uid) / f"{project_id}.json"
    if not p.exists():
        raise HTTPException(404, "Project not found")
    return _json.loads(p.read_text())


@router.put("/{project_id}")
async def update_project(project_id: str, request: Request):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    p = _proj_dir(user_id) / f"{project_id}.json"
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
):
    uid = _resolve_user_id(user_id, authorization)
    p = _proj_dir(uid) / f"{project_id}.json"
    if not p.exists():
        raise HTTPException(404, "Project not found")
    p.unlink()
    return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/members")
async def add_member(project_id: str, request: Request):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    agent_name = body.get("agent_name", "")
    p = _proj_dir(user_id) / f"{project_id}.json"
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
):
    uid = _resolve_user_id(user_id, authorization)
    p = _proj_dir(uid) / f"{project_id}.json"
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
):
    uid = _resolve_user_id(user_id, authorization)
    return {"tasks": _load_tasks(project_id, uid)}


@router.post("/{project_id}/tasks")
async def create_task(project_id: str, request: Request):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    tasks = _load_tasks(project_id, user_id)
    task = {
        "id": str(uuid.uuid4())[:8],
        "project_id": project_id,
        "title": body.get("title", ""),
        "description": body.get("description", ""),
        "status": "todo",
        "assigned_agent": body.get("assigned_agent"),
        "priority": body.get("priority", "medium"),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    tasks.append(task)
    _save_tasks(project_id, tasks, user_id)
    return task


@router.put("/{project_id}/tasks/{task_id}")
async def update_task(project_id: str, task_id: str, request: Request):
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    tasks = _load_tasks(project_id, user_id)
    for t in tasks:
        if t["id"] == task_id:
            for k in ("title", "description", "status", "assigned_agent", "priority"):
                if k in body:
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
):
    """获取项目的 agent-card.json 内容 (成员能力快照)."""
    uid = _resolve_user_id(user_id, authorization)
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
):
    uid = _resolve_user_id(user_id, authorization)
    tasks = _load_tasks(project_id, uid)
    tasks = [t for t in tasks if t["id"] != task_id]
    _save_tasks(project_id, tasks, uid)
    return {"status": "deleted", "id": task_id}
