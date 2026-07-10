"""Harness API route definitions."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from harness.agents.presets import PRESET_SUBAGENTS
from harness.api.server import HarnessService, get_harness
from harness.models import (
    ClarificationResponse,
    ExecuteRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@router.post("/execute")
async def execute(
    request: ExecuteRequest,
    harness: HarnessService = Depends(get_harness),
):
    """Execute an agent task with SSE streaming output."""

    async def event_stream():
        async for event in harness.execute(
            thread_id=request.thread_id,
            user_id=request.user_id,
            message=request.message,
            graph=request.execution_graph,
            files=request.files,
            project_id=request.project_id,
            agent_name=request.agent_name,
            mode=request.mode,
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/execute/{thread_id}/respond")
async def respond_clarification(
    thread_id: str,
    request: ClarificationResponse,
    harness: HarnessService = Depends(get_harness),
):
    """Respond to a pending clarification request — streams resumed execution."""

    async def event_stream():
        async for event in harness.respond_to_clarification(
            thread_id=thread_id,
            answer=request.answer,
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stop/{thread_id}")
async def stop_execution(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Cancel a running execution."""
    await harness.stop(thread_id)
    return {"status": "stopped", "thread_id": thread_id}


@router.get("/status/{thread_id}")
async def get_status(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Return the current execution status of a thread."""
    return await harness.get_status(thread_id)


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str,
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """Delete all persisted data for a thread (checkpoint + workspace)."""
    return await harness.delete_thread(thread_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Agent management (persistent per-user agents with SOUL.md + config.yaml)
# ---------------------------------------------------------------------------


@router.get("/agents")
async def list_agents(
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """List all custom agents for a user."""
    from harness.config.agents_config import list_custom_agents
    agents = list_custom_agents(user_id=user_id)
    return {
        "agents": [a.model_dump() for a in agents],
        "count": len(agents),
    }


@router.post("/agents")
async def create_agent(
    request: Request,
    harness: HarnessService = Depends(get_harness),
):
    """Create a new agent (body: name, display_name, description, soul, model, tool_groups, skills)."""
    from harness.config.agents_config import (
        AgentConfig,
        save_agent_config,
        save_agent_soul,
        validate_agent_name,
    )
    body = await request.json()
    name = validate_agent_name(body.get("name", ""))
    soul = body.get("soul", "")
    cfg = AgentConfig(
        name=name,
        display_name=body.get("display_name", name),
        description=body.get("description", ""),
        model=body.get("model", "inherit"),
        tool_groups=body.get("tool_groups", []),
        skills=body.get("skills"),
        memory_scope=body.get("memory_scope", "agent"),
        can_be_lead=body.get("can_be_lead", True),
        can_delegate=body.get("can_delegate", True),
        max_turns=body.get("max_turns", 50),
        timeout_seconds=body.get("timeout_seconds", 900),
        isolation=body.get("isolation", "none"),
    )
    user_id = body.get("user_id", "default")
    save_agent_config(name, cfg, user_id=user_id)
    if soul:
        save_agent_soul(name, soul, user_id=user_id)
    return {"status": "created", "name": name}


@router.get("/agents/{name}")
async def get_agent(
    name: str,
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """Get a single agent's config + SOUL."""
    from harness.config.agents_config import (
        load_agent_config,
        load_agent_soul,
    )
    cfg = load_agent_config(name, user_id=user_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    soul = load_agent_soul(name, user_id=user_id)
    return {"agent": cfg.model_dump(), "soul": soul}


@router.put("/agents/{name}")
async def update_agent(
    name: str,
    request: Request,
    harness: HarnessService = Depends(get_harness),
):
    """Update agent config + SOUL."""
    from harness.config.agents_config import (
        AgentConfig,
        load_agent_config,
        save_agent_config,
        save_agent_soul,
    )
    body = await request.json()
    user_id = body.get("user_id", "default")
    existing = load_agent_config(name, user_id=user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    cfg = AgentConfig(
        name=name,
        display_name=body.get("display_name", existing.display_name),
        description=body.get("description", existing.description),
        model=body.get("model", existing.model),
        tool_groups=body.get("tool_groups", existing.tool_groups),
        skills=body.get("skills", existing.skills),
        memory_scope=body.get("memory_scope", existing.memory_scope),
        can_be_lead=body.get("can_be_lead", existing.can_be_lead),
        can_delegate=body.get("can_delegate", existing.can_delegate),
        max_turns=body.get("max_turns", existing.max_turns),
        timeout_seconds=body.get("timeout_seconds", existing.timeout_seconds),
        isolation=body.get("isolation", existing.isolation),
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )
    save_agent_config(name, cfg, user_id=user_id)
    if "soul" in body:
        save_agent_soul(name, body["soul"], user_id=user_id)
    return {"status": "updated", "name": name}


@router.delete("/agents/{name}")
async def delete_agent(
    name: str,
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """Delete an agent and its directory."""
    from harness.config.agents_config import delete_agent
    ok = delete_agent(name, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"status": "deleted", "name": name}


@router.get("/agents/{name}/memory")
async def get_agent_memory(
    name: str,
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """Get agent memory data."""
    from harness.memory.updater import get_memory_data
    data = get_memory_data(agent_name=name, user_id=user_id)
    return {"name": name, "memory": data}


@router.delete("/agents/{name}/memory")
async def clear_agent_memory(
    name: str,
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """Clear agent memory."""
    from harness.memory.updater import clear_memory_data
    clear_memory_data(agent_name=name, user_id=user_id)
    return {"status": "cleared", "name": name}


# ---------------------------------------------------------------------------
# Projects (agent team collaboration)
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_projects(
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """List all projects for a user."""
    from harness.config.paths import get_paths
    import json as _json
    paths = get_paths()
    projects_dir = paths.base_dir / "users" / user_id / "projects"
    if not projects_dir.exists():
        return {"projects": [], "count": 0}
    projects = []
    for f in sorted(projects_dir.glob("*.json")):
        try:
            with open(f) as fp:
                projects.append(_json.load(fp))
        except Exception:
            pass
    return {"projects": projects, "count": len(projects)}


@router.post("/projects")
async def create_project(
    request: Request,
    harness: HarnessService = Depends(get_harness),
):
    """Create a new project."""
    from harness.config.paths import get_paths
    import json as _json
    import uuid
    body = await request.json()
    user_id = body.get("user_id", "default")
    paths = get_paths()
    projects_dir = paths.base_dir / "users" / user_id / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
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
    proj_path = projects_dir / f"{pid}.json"
    with open(proj_path, "w") as fp:
        _json.dump(project, fp, indent=2)
    return project


@router.get("/projects/{project_id}")
async def get_project(project_id: str, user_id: str = "default"):
    """Get a single project."""
    from harness.config.paths import get_paths
    import json as _json
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(proj_path) as fp:
        return _json.load(fp)


@router.put("/projects/{project_id}")
async def update_project(project_id: str, request: Request, harness: HarnessService = Depends(get_harness)):
    """Update project metadata."""
    from harness.config.paths import get_paths
    import json as _json
    body = await request.json()
    user_id = body.get("user_id", "default")
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(proj_path) as fp:
        project = _json.load(fp)
    for key in ("name", "description", "members"):
        if key in body:
            project[key] = body[key]
    project["updated_at"] = datetime.now().isoformat()
    with open(proj_path, "w") as fp:
        _json.dump(project, fp, indent=2)
    return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, user_id: str = "default"):
    """Delete a project."""
    from harness.config.paths import get_paths
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    proj_path.unlink()
    return {"status": "deleted", "id": project_id}


@router.post("/projects/{project_id}/members")
async def add_project_member(project_id: str, request: Request, harness: HarnessService = Depends(get_harness)):
    """Add an agent to a project."""
    from harness.config.paths import get_paths
    import json as _json
    body = await request.json()
    user_id = body.get("user_id", "default")
    agent_name = body.get("agent_name", "")
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(proj_path) as fp:
        project = _json.load(fp)
    members = project.get("members", [])
    if agent_name not in members:
        members.append(agent_name)
    project["members"] = members
    project["updated_at"] = datetime.now().isoformat()
    with open(proj_path, "w") as fp:
        _json.dump(project, fp, indent=2)
    return project


@router.delete("/projects/{project_id}/members/{agent_name}")
async def remove_project_member(project_id: str, agent_name: str, user_id: str = "default"):
    """Remove an agent from a project."""
    from harness.config.paths import get_paths
    import json as _json
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(proj_path) as fp:
        project = _json.load(fp)
    members = [m for m in project.get("members", []) if m != agent_name]
    project["members"] = members
    project["updated_at"] = datetime.now().isoformat()
    with open(proj_path, "w") as fp:
        _json.dump(project, fp, indent=2)
    return project


# ---------------------------------------------------------------------------
# Tasks (per-project task board)
# ---------------------------------------------------------------------------

_tasks_dir: dict[str, Path] = {}

def _get_tasks_path(project_id: str, user_id: str = "default") -> Path:
    from harness.config.paths import get_paths
    paths = get_paths()
    tasks_dir = paths.base_dir / "users" / user_id / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir / f"{project_id}.json"


def _load_tasks(project_id: str, user_id: str = "default") -> list[dict]:
    path = _get_tasks_path(project_id, user_id)
    if not path.exists():
        return []
    import json as _json
    with open(path) as fp:
        return _json.load(fp)


def _save_tasks(project_id: str, tasks: list[dict], user_id: str = "default") -> None:
    path = _get_tasks_path(project_id, user_id)
    import json as _json
    with open(path, "w") as fp:
        _json.dump(tasks, fp, indent=2)


@router.get("/projects/{project_id}/tasks")
async def list_tasks(project_id: str, user_id: str = "default"):
    """List tasks for a project."""
    return {"tasks": _load_tasks(project_id, user_id)}


@router.post("/projects/{project_id}/tasks")
async def create_task(project_id: str, request: Request, harness: HarnessService = Depends(get_harness)):
    """Create a new task."""
    import uuid
    body = await request.json()
    user_id = body.get("user_id", "default")
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
    # Update project task_count
    _update_project_task_count(project_id, len(tasks), user_id)
    return task


@router.put("/projects/{project_id}/tasks/{task_id}")
async def update_task(project_id: str, task_id: str, request: Request, harness: HarnessService = Depends(get_harness)):
    """Update a task."""
    body = await request.json()
    user_id = body.get("user_id", "default")
    tasks = _load_tasks(project_id, user_id)
    for t in tasks:
        if t["id"] == task_id:
            for key in ("title", "description", "status", "assigned_agent", "priority"):
                if key in body:
                    t[key] = body[key]
            t["updated_at"] = datetime.now().isoformat()
            _save_tasks(project_id, tasks, user_id)
            return t
    raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/projects/{project_id}/tasks/{task_id}")
async def delete_task(project_id: str, task_id: str, user_id: str = "default"):
    """Delete a task."""
    tasks = _load_tasks(project_id, user_id)
    tasks = [t for t in tasks if t["id"] != task_id]
    _save_tasks(project_id, tasks, user_id)
    _update_project_task_count(project_id, len(tasks), user_id)
    return {"status": "deleted", "id": task_id}


def _update_project_task_count(project_id: str, count: int, user_id: str = "default") -> None:
    from harness.config.paths import get_paths
    import json as _json
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if proj_path.exists():
        with open(proj_path) as fp:
            project = _json.load(fp)
        project["task_count"] = count
        project["updated_at"] = datetime.now().isoformat()
        with open(proj_path, "w") as fp:
            _json.dump(project, fp, indent=2)


# ---------------------------------------------------------------------------
# Preset agents (read-only templates)
# ---------------------------------------------------------------------------


@router.get("/agents/presets")
async def get_preset_agents():
    """Return predefined SubAgent templates."""
    return PRESET_SUBAGENTS


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@router.get("/traces/{thread_id}")
async def get_trace(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Get trace details for a thread."""
    return harness.observability.get_trace(thread_id)


@router.get("/metrics/token-usage")
async def get_token_usage(
    user_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    harness: HarnessService = Depends(get_harness),
):
    """Get token consumption statistics."""
    return harness.observability.get_token_usage(user_id, start_date, end_date)


# ---------------------------------------------------------------------------
# Tool Groups
# ---------------------------------------------------------------------------


@router.get("/tool-groups")
async def get_tool_groups(
    harness: HarnessService = Depends(get_harness),
):
    """Return the available tool groups."""
    return harness.tool_registry.setup_tool_groups()
