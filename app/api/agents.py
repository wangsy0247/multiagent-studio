"""Agent 管理 API — per-user persistent agents (SOUL.md + config.yaml)."""

import logging
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent管理"])


@router.get("")
async def list_agents(user_id: str = "default"):
    from harness.config.agents_config import list_custom_agents
    agents = list_custom_agents(user_id=user_id)
    return {"agents": [a.model_dump() for a in agents], "count": len(agents)}


@router.post("")
async def create_agent(request: Request):
    from harness.config.agents_config import AgentConfig, save_agent_config, save_agent_soul, validate_agent_name
    body = await request.json()
    name = validate_agent_name(body.get("name", ""))
    soul = body.get("soul", "")
    cfg = AgentConfig(
        name=name, display_name=body.get("display_name", name),
        description=body.get("description", ""), model=body.get("model", "inherit"),
        tool_groups=body.get("tool_groups", []), skills=body.get("skills"),
    )
    user_id = body.get("user_id", "default")
    save_agent_config(name, cfg, user_id=user_id)
    if soul: save_agent_soul(name, soul, user_id=user_id)
    return {"status": "created", "name": name}


@router.get("/{name}")
async def get_agent(name: str, user_id: str = "default"):
    from harness.config.agents_config import load_agent_config, load_agent_soul
    cfg = load_agent_config(name, user_id=user_id)
    if cfg is None: raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"agent": cfg.model_dump(), "soul": load_agent_soul(name, user_id=user_id)}


@router.put("/{name}")
async def update_agent(name: str, request: Request):
    from harness.config.agents_config import AgentConfig, load_agent_config, save_agent_config, save_agent_soul
    body = await request.json()
    user_id = body.get("user_id", "default")
    existing = load_agent_config(name, user_id=user_id)
    if existing is None: raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    cfg = AgentConfig(
        name=name, display_name=body.get("display_name", existing.display_name),
        description=body.get("description", existing.description),
        model=body.get("model", existing.model),
        tool_groups=body.get("tool_groups", existing.tool_groups),
        skills=body.get("skills", existing.skills),
        created_at=existing.created_at, updated_at=existing.updated_at,
    )
    save_agent_config(name, cfg, user_id=user_id)
    if "soul" in body: save_agent_soul(name, body["soul"], user_id=user_id)
    return {"status": "updated", "name": name}


@router.delete("/{name}")
async def delete_agent(name: str, user_id: str = "default"):
    from harness.config.agents_config import delete_agent
    if not delete_agent(name, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"status": "deleted", "name": name}


@router.get("/{name}/memory")
async def get_agent_memory(name: str, user_id: str = "default"):
    from harness.memory.updater import get_memory_data
    return {"name": name, "memory": get_memory_data(agent_name=name, user_id=user_id)}


@router.delete("/{name}/memory")
async def clear_agent_memory(name: str, user_id: str = "default"):
    from harness.memory.updater import clear_memory_data
    clear_memory_data(agent_name=name, user_id=user_id)
    return {"status": "cleared", "name": name}
