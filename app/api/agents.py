"""Agent 管理 API — per-user persistent agents (SOUL.md + config.yaml)."""

import logging
from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent管理"])


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
    """解析真实 user_id：
    1. explicit 非空且不是 "default" → 直接使用
    2. 否则尝试从 JWT token 提取
    3. 都失败 → "default"
    """
    if explicit and explicit != "default":
        return explicit
    jwt_uid = _extract_jwt_sub(authorization)
    if jwt_uid:
        logger.info(f"[_resolve_user_id] JWT 兜底 → user_id={jwt_uid}")
        return jwt_uid
    return "default"


@router.get("")
async def list_agents(
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.config.agents_config import list_custom_agents
    uid = _resolve_user_id(user_id, authorization)
    agents = list_custom_agents(user_id=uid)
    return {"agents": [a.model_dump() for a in agents], "count": len(agents)}


@router.post("")
async def create_agent(request: Request):
    from harness.config.agents_config import AgentConfig, save_agent_config, save_agent_soul, validate_agent_name
    body = await request.json()
    logger.info(f"[create_agent] body user_id={body.get('user_id', 'NOT_SET')}")
    name = validate_agent_name(body.get("name", ""))
    soul = body.get("soul", "")
    cfg = AgentConfig(
        name=name, display_name=body.get("display_name", name),
        description=body.get("description", ""), model=body.get("model", "inherit"),
        tool_groups=body.get("tool_groups", []), skills=body.get("skills"),
        memory_scope=body.get("memory_scope", "agent"),
        can_be_lead=body.get("can_be_lead", False),
        can_delegate=body.get("can_delegate", True),
        max_turns=body.get("max_turns", 50),
        timeout_seconds=body.get("timeout_seconds", 900),
        isolation=body.get("isolation", "none"),
    )
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    logger.info(f"[create_agent] 最终 user_id={user_id}, name={name}")
    save_agent_config(name, cfg, user_id=user_id)
    if soul: save_agent_soul(name, soul, user_id=user_id)
    return {"status": "created", "name": name}


@router.get("/{name}")
async def get_agent(
    name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.config.agents_config import load_agent_config, load_agent_soul
    uid = _resolve_user_id(user_id, authorization)
    cfg = load_agent_config(name, user_id=uid)
    if cfg is None: raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"agent": cfg.model_dump(), "soul": load_agent_soul(name, user_id=uid)}


@router.put("/{name}")
async def update_agent(name: str, request: Request):
    from harness.config.agents_config import AgentConfig, load_agent_config, save_agent_config, save_agent_soul
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    existing = load_agent_config(name, user_id=user_id)
    if existing is None: raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    cfg = AgentConfig(
        name=name, display_name=body.get("display_name", existing.display_name),
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
        created_at=existing.created_at, updated_at=existing.updated_at,
    )
    save_agent_config(name, cfg, user_id=user_id)
    if "soul" in body: save_agent_soul(name, body["soul"], user_id=user_id)
    return {"status": "updated", "name": name}


@router.delete("/{name}")
async def delete_agent(
    name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.config.agents_config import delete_agent
    uid = _resolve_user_id(user_id, authorization)
    if not delete_agent(name, user_id=uid):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"status": "deleted", "name": name}


@router.get("/{name}/memory")
async def get_agent_memory(
    name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.memory.updater import get_memory_data
    uid = _resolve_user_id(user_id, authorization)
    return {"name": name, "memory": get_memory_data(agent_name=name, user_id=uid)}


@router.delete("/{name}/memory")
async def clear_agent_memory(
    name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.memory.updater import clear_memory_data
    uid = _resolve_user_id(user_id, authorization)
    clear_memory_data(agent_name=name, user_id=uid)
    return {"status": "cleared", "name": name}
