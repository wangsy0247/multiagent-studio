"""项目 + 任务面板 API."""

import json as _json
import uuid
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

from harness.config.paths import get_paths

logger = __import__("logging").getLogger(__name__)
router = APIRouter(tags=["项目管理"])

def _proj_dir(user_id: str) -> Path:
    return get_paths().base_dir / "users" / user_id / "projects"

def _tasks_path(project_id: str, user_id: str) -> Path:
    d = get_paths().base_dir / "users" / user_id / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project_id}.json"

def _load_tasks(project_id: str, user_id: str) -> list:
    p = _tasks_path(project_id, user_id)
    if not p.exists(): return []
    return _json.loads(p.read_text())

def _save_tasks(project_id: str, tasks: list, user_id: str):
    _tasks_path(project_id, user_id).write_text(_json.dumps(tasks, indent=2))


@router.get("")
async def list_projects(user_id: str = "default"):
    d = _proj_dir(user_id)
    if not d.exists(): return {"projects": [], "count": 0}
    projects = []
    for f in sorted(d.glob("*.json")):
        try: projects.append(_json.loads(f.read_text()))
        except Exception: pass
    return {"projects": projects, "count": len(projects)}


@router.post("")
async def create_project(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "default")
    d = _proj_dir(user_id); d.mkdir(parents=True, exist_ok=True)
    pid = body.get("id", str(uuid.uuid4())[:8])
    now = datetime.now().isoformat()
    project = {"id": pid, "name": body.get("name", "New Project"),
               "description": body.get("description", ""),
               "members": body.get("members", []), "thread_count": 0, "task_count": 0,
               "created_at": now, "updated_at": now}
    (d / f"{pid}.json").write_text(_json.dumps(project, indent=2))
    return project


@router.get("/{project_id}")
async def get_project(project_id: str, user_id: str = "default"):
    p = _proj_dir(user_id) / f"{project_id}.json"
    if not p.exists(): raise HTTPException(404, "Project not found")
    return _json.loads(p.read_text())


@router.put("/{project_id}")
async def update_project(project_id: str, request: Request):
    body = await request.json()
    user_id = body.get("user_id", "default")
    p = _proj_dir(user_id) / f"{project_id}.json"
    if not p.exists(): raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    for k in ("name", "description", "members"):
        if k in body: project[k] = body[k]
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.delete("/{project_id}")
async def delete_project(project_id: str, user_id: str = "default"):
    p = _proj_dir(user_id) / f"{project_id}.json"
    if not p.exists(): raise HTTPException(404, "Project not found")
    p.unlink()
    return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/members")
async def add_member(project_id: str, request: Request):
    body = await request.json()
    user_id = body.get("user_id", "default")
    agent_name = body.get("agent_name", "")
    p = _proj_dir(user_id) / f"{project_id}.json"
    if not p.exists(): raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    members = project.get("members", [])
    if agent_name not in members: members.append(agent_name)
    project["members"] = members
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.delete("/{project_id}/members/{agent_name}")
async def remove_member(project_id: str, agent_name: str, user_id: str = "default"):
    p = _proj_dir(user_id) / f"{project_id}.json"
    if not p.exists(): raise HTTPException(404, "Project not found")
    project = _json.loads(p.read_text())
    project["members"] = [m for m in project.get("members", []) if m != agent_name]
    project["updated_at"] = datetime.now().isoformat()
    p.write_text(_json.dumps(project, indent=2))
    return project


@router.get("/{project_id}/tasks")
async def list_tasks(project_id: str, user_id: str = "default"):
    return {"tasks": _load_tasks(project_id, user_id)}


@router.post("/{project_id}/tasks")
async def create_task(project_id: str, request: Request):
    body = await request.json()
    user_id = body.get("user_id", "default")
    tasks = _load_tasks(project_id, user_id)
    task = {"id": str(uuid.uuid4())[:8], "project_id": project_id,
            "title": body.get("title", ""), "description": body.get("description", ""),
            "status": "todo", "assigned_agent": body.get("assigned_agent"),
            "priority": body.get("priority", "medium"),
            "created_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat()}
    tasks.append(task)
    _save_tasks(project_id, tasks, user_id)
    return task


@router.put("/{project_id}/tasks/{task_id}")
async def update_task(project_id: str, task_id: str, request: Request):
    body = await request.json()
    user_id = body.get("user_id", "default")
    tasks = _load_tasks(project_id, user_id)
    for t in tasks:
        if t["id"] == task_id:
            for k in ("title", "description", "status", "assigned_agent", "priority"):
                if k in body: t[k] = body[k]
            t["updated_at"] = datetime.now().isoformat()
            _save_tasks(project_id, tasks, user_id)
            return t
    raise HTTPException(404, "Task not found")


@router.delete("/{project_id}/tasks/{task_id}")
async def delete_task(project_id: str, task_id: str, user_id: str = "default"):
    tasks = _load_tasks(project_id, user_id)
    tasks = [t for t in tasks if t["id"] != task_id]
    _save_tasks(project_id, tasks, user_id)
    return {"status": "deleted", "id": task_id}
