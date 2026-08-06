"""项目 + 任务面板 API."""

import json as _json
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.engine import get_db
from app.models.user import User
from harness.config.paths import get_paths

logger = logging.getLogger(__name__)
router = APIRouter(tags=["项目管理"])

# 路径段白名单 — project_id / thread_id / agent_name 只允许安全字符,
# 阻断 ".." 与目录分隔符造成的路径穿越 (会拼接进文件路径)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _validate_id(value: str, kind: str = "id") -> str:
    """校验路径段只含安全字符, 否则 400."""
    if not value or not _SAFE_ID_RE.match(value):
        raise HTTPException(400, f"Invalid {kind}: {value!r}")
    return value


# ── 路径工具 ──

def _proj_dir(user_id: str) -> Path:
    """用户项目根目录: {base}/users/{uid}/projects/."""
    _validate_id(user_id, "user_id")
    return get_paths().base_dir / "users" / user_id / "projects"


def _project_file(project_id: str, user_id: str) -> Path:
    """单个项目的元数据文件: {base}/users/{uid}/projects/{pid}/project.json.

    向后兼容旧格式 projects/{pid}.json: 若存在则自动迁移到新格式.
    """
    _validate_id(project_id, "project_id")
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
    _validate_id(project_id, "project_id")
    return _proj_dir(user_id) / project_id


def _tasks_path(project_id: str, user_id: str, thread_id: str = "") -> Path:
    """项目任务文件: {base}/users/{uid}/projects/{pid}/threads/{tid}/tasks.json."""
    if thread_id:
        _validate_id(thread_id, "thread_id")
    d = _project_dir(project_id, user_id) / "threads" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"


def _load_tasks(project_id: str, user_id: str, thread_id: str = "") -> list:
    p = _tasks_path(project_id, user_id, thread_id)
    if not p.exists():
        return []
    return _json.loads(p.read_text())


def _save_tasks(project_id: str, tasks: list, user_id: str, thread_id: str = ""):
    _tasks_path(project_id, user_id, thread_id).write_text(_json.dumps(tasks, indent=2))


# ── 路由 ──

@router.get("")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
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
async def create_project(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = await request.json()
    user_id = current_user.username
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
    p = _project_file(project_id, uid)
    if not p.exists():
        raise HTTPException(404, "Project not found")
    return _json.loads(p.read_text())


@router.put("/{project_id}")
async def update_project(project_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = await request.json()
    user_id = current_user.username
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
    # ── Phase 6: 回收该项目登记的 team worktree (仅 team- 前缀, 容错不阻断删除) ──
    try:
        from harness.team.worktree import cleanup_project_worktrees
        removed = await cleanup_project_worktrees(project_id, uid)
        if removed:
            logger.info("回收项目 '%s' 的 %d 个 team worktree", project_id, removed)
    except Exception:
        logger.warning(
            "项目 '%s' 的 worktree 回收失败 — 继续删除项目", project_id,
            exc_info=True,
        )
    # 删除新格式目录 (如果存在); resolve 围栏双保险, 防穿越删除
    d = _project_dir(project_id, uid)
    base = _proj_dir(uid).resolve()
    if d.exists():
        if base not in d.resolve().parents:
            raise HTTPException(400, "Invalid project path")
        shutil.rmtree(d)
        deleted = True
    else:
        deleted = False
    # 兼容旧格式文件
    old = _proj_dir(uid) / f"{project_id}.json"
    if old.exists():
        old.unlink()
        deleted = True
    if not deleted:
        raise HTTPException(404, "Project not found")
    return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/members")
async def add_member(project_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = await request.json()
    user_id = current_user.username
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
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
    thread_id: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
    return {"tasks": _load_tasks(project_id, uid, thread_id)}


@router.post("/{project_id}/tasks")
async def create_task(project_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = await request.json()
    user_id = current_user.username
    thread_id = body.get("thread_id", "")
    tasks = _load_tasks(project_id, user_id, thread_id)
    # 校验 status (与后端 TeamTaskStatus 8 态保持一致)
    valid_statuses = {"pending", "in_progress", "in_review", "approved",
                      "revision_needed", "completed", "failed", "cancelled"}
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
    _save_tasks(project_id, tasks, user_id, thread_id)
    return task


@router.put("/{project_id}/tasks/{task_id}")
async def update_task(project_id: str, task_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = await request.json()
    user_id = current_user.username
    thread_id = body.get("thread_id", "")
    tasks = _load_tasks(project_id, user_id, thread_id)
    for t in tasks:
        if t["id"] == task_id:
            for k in ("title", "description", "status", "assigned_agent", "priority"):
                if k in body:
                    # 校验 status 合法性
                    if k == "status":
                        valid_statuses = {"pending", "in_progress", "in_review", "approved",
                                          "revision_needed", "completed", "failed", "cancelled"}
                        if body[k] not in valid_statuses:
                            raise HTTPException(400, f"Invalid status: {body[k]}. Must be one of {valid_statuses}")
                    t[k] = body[k]
            t["updated_at"] = datetime.now().isoformat()
            _save_tasks(project_id, tasks, user_id, thread_id)
            return t
    raise HTTPException(404, "Task not found")


# ── Agent 对话日志 (前端按 agent 隔离展示工作内容) ──


@router.get("/{project_id}/agent-logs/{thread_id}")
async def list_agent_logs(
    project_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出某个 team thread 下所有 agent 的对话日志摘要."""
    uid = current_user.username
    _validate_id(project_id, "project_id")
    _validate_id(thread_id, "thread_id")
    logs_dir = get_paths().agent_logs_dir(thread_id, project_id, user_id=uid)
    if not logs_dir.exists():
        return {"thread_id": thread_id, "agents": [], "count": 0}

    agents: list[dict] = []
    for f in sorted(logs_dir.glob("*.jsonl")):
        try:
            lines = [
                _json.loads(line)
                for line in f.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            task_ids = list({
                entry["task_id"]
                for entry in lines
                if entry.get("task_id")
            })
            agents.append({
                "agent_name": f.stem,
                "task_count": len(task_ids),
                "entry_count": len(lines),
                "size_bytes": f.stat().st_size,
            })
        except Exception:
            logger.exception("Failed to read agent log: %s", f)
            agents.append({
                "agent_name": f.stem,
                "task_count": 0,
                "entry_count": 0,
                "size_bytes": 0,
                "error": "读取失败",
            })
    return {"thread_id": thread_id, "agents": agents, "count": len(agents)}


@router.get("/{project_id}/agent-logs/{thread_id}/{agent_name}")
async def get_agent_log(
    project_id: str,
    thread_id: str,
    agent_name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """读取某个 agent 在指定 thread 中的完整对话日志 (JSONL → JSON array)."""
    uid = current_user.username
    _validate_id(project_id, "project_id")
    _validate_id(thread_id, "thread_id")
    _validate_id(agent_name, "agent_name")
    log_file = (
        get_paths().agent_logs_dir(thread_id, project_id, user_id=uid)
        / f"{agent_name}.jsonl"
    )
    if not log_file.exists():
        return {"agent_name": agent_name, "thread_id": thread_id, "entries": [], "count": 0}

    try:
        entries = [
            _json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception:
        logger.exception("Failed to parse agent log: %s", log_file)
        raise HTTPException(500, "Failed to read agent log")

    return {
        "agent_name": agent_name,
        "thread_id": thread_id,
        "entries": entries,
        "count": len(entries),
    }


# ── Agent Cards ──

@router.get("/{project_id}/agent-cards")
async def get_agent_cards(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取项目的 agent-card.json 内容 (成员能力快照)."""
    uid = current_user.username
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
    thread_id: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.username
    tasks = _load_tasks(project_id, uid, thread_id)
    tasks = [t for t in tasks if t["id"] != task_id]
    _save_tasks(project_id, tasks, uid, thread_id)
    return {"status": "deleted", "id": task_id}
