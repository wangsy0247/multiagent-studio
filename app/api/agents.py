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
    from harness.config.agents_config import (
        AgentConfig, AgentMemoryFields, AgentFeaturesFields,
        AgentLimitsFields, AgentTeamFields, AgentSubagentsFields,
        save_agent_config, save_agent_soul, save_agent_extensions, validate_agent_name,
    )
    body = await request.json()
    logger.info(f"[create_agent] body user_id={body.get('user_id', 'NOT_SET')}")
    try:
        name = validate_agent_name(body.get("name", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # model 必选
    model = body.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail="'model' 字段为必选项 — 请指定 Agent 使用的模型 (如 'gpt-4o')")

    soul = body.get("soul", "")

    # 解析子模型 (如果请求体包含嵌套结构, 否则使用平铺字段自动映射)
    mem_data = body.get("memory", {})
    feat_data = body.get("features", {})
    lim_data = body.get("limits", {})
    team_data = body.get("team", {})
    sub_data = body.get("subagents", {})

    cfg = AgentConfig(
        name=name,
        display_name=body.get("display_name", name),
        description=body.get("description", ""),
        model=model,
        temperature=body.get("temperature", 0.3),
        max_tokens=body.get("max_tokens", 4096),
        tool_groups=body.get("tool_groups", []),
        skills=body.get("skills", []),
        memory=AgentMemoryFields(
            backend=mem_data.get("backend", "file"),
            max_facts=mem_data.get("max_facts", 10),
            injection_enabled=mem_data.get("injection_enabled", True),
            max_injection_tokens=mem_data.get("max_injection_tokens", 500),
            mem0_tool_enabled=mem_data.get("mem0_tool_enabled", False),
            mem0_search_top_k=mem_data.get("mem0_search_top_k", 5),
        ),
        features=AgentFeaturesFields(
            summarization=feat_data.get("summarization", True),
            subagent=feat_data.get("subagent", True),
            langfuse=feat_data.get("langfuse", True),
            guardrail=feat_data.get("guardrail", False),
        ),
        limits=AgentLimitsFields(
            max_turns=body.get("max_turns", lim_data.get("max_turns", 50)),
            timeout_seconds=body.get("timeout_seconds", lim_data.get("timeout_seconds", 900)),
        ),
        team=AgentTeamFields(
            can_be_lead=body.get("can_be_lead", team_data.get("can_be_lead", False)),
            can_delegate=body.get("can_delegate", team_data.get("can_delegate", True)),
            memory_scope=body.get("memory_scope", team_data.get("memory_scope", "agent")),
        ),
        subagents=AgentSubagentsFields(
            timeout_seconds=sub_data.get("timeout_seconds", 900),
            max_concurrent=sub_data.get("max_concurrent", 3),
        ),
    )
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    logger.info(f"[create_agent] 最终 user_id={user_id}, name={name}, model={model}")
    save_agent_config(name, cfg, user_id=user_id)
    if soul:
        save_agent_soul(name, soul, user_id=user_id)

    # 自动生成 extensions_config.yaml 模板
    mcp_servers_data = body.get("mcp_servers", {})
    save_agent_extensions(
        name,
        mcp_servers=mcp_servers_data if mcp_servers_data else {},
        user_id=user_id,
    )
    return {"status": "created", "name": name, "model": model}


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
    from harness.config.agents_config import (
        AgentConfig, AgentMemoryFields, AgentFeaturesFields,
        AgentLimitsFields, AgentTeamFields, AgentSubagentsFields,
        load_agent_config, save_agent_config, save_agent_soul,
    )
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    existing = load_agent_config(name, user_id=user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    # 合并子模型 — 新值覆盖已有值
    mem_data = body.get("memory", {})
    feat_data = body.get("features", {})
    lim_data = body.get("limits", {})
    team_data = body.get("team", {})
    sub_data = body.get("subagents", {})

    cfg = AgentConfig(
        name=name,
        display_name=body.get("display_name", existing.display_name),
        description=body.get("description", existing.description),
        model=body.get("model", existing.model),
        temperature=body.get("temperature", existing.temperature),
        max_tokens=body.get("max_tokens", existing.max_tokens),
        tool_groups=body.get("tool_groups", existing.tool_groups),
        skills=body.get("skills", existing.skills),
        memory=AgentMemoryFields(
            backend=mem_data.get("backend", existing.memory.backend),
            max_facts=mem_data.get("max_facts", existing.memory.max_facts),
            injection_enabled=mem_data.get("injection_enabled", existing.memory.injection_enabled),
            max_injection_tokens=mem_data.get("max_injection_tokens", existing.memory.max_injection_tokens),
            mem0_tool_enabled=mem_data.get("mem0_tool_enabled", existing.memory.mem0_tool_enabled),
            mem0_search_top_k=mem_data.get("mem0_search_top_k", existing.memory.mem0_search_top_k),
        ),
        features=AgentFeaturesFields(
            summarization=feat_data.get("summarization", existing.features.summarization),
            subagent=feat_data.get("subagent", existing.features.subagent),
            langfuse=feat_data.get("langfuse", existing.features.langfuse),
            guardrail=feat_data.get("guardrail", existing.features.guardrail),
        ),
        limits=AgentLimitsFields(
            max_turns=lim_data.get("max_turns", existing.limits.max_turns),
            timeout_seconds=lim_data.get("timeout_seconds", existing.limits.timeout_seconds),
        ),
        team=AgentTeamFields(
            can_be_lead=team_data.get("can_be_lead", existing.team.can_be_lead),
            can_delegate=team_data.get("can_delegate", existing.team.can_delegate),
            memory_scope=team_data.get("memory_scope", existing.team.memory_scope),
        ),
        subagents=AgentSubagentsFields(
            timeout_seconds=sub_data.get("timeout_seconds", existing.subagents.timeout_seconds),
            max_concurrent=sub_data.get("max_concurrent", existing.subagents.max_concurrent),
        ),
        created_at=existing.created_at,
        updated_at="",
    )
    save_agent_config(name, cfg, user_id=user_id)
    if "soul" in body:
        save_agent_soul(name, body["soul"], user_id=user_id)
    return {"status": "updated", "name": name}


@router.delete("/{name}")
async def delete_agent(
    name: str,
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    from harness.config.agents_config import delete_agent, is_default_agent
    if is_default_agent(name):
        raise HTTPException(status_code=403, detail="Cannot delete the 'default' agent — it is required by the system")
    uid = _resolve_user_id(user_id, authorization)
    if not delete_agent(name, user_id=uid):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"status": "deleted", "name": name}


# ── 用户全局配置 ──
@router.get("/config/global")
async def get_user_global_config(
    user_id: str = "default",
    authorization: str | None = Header(None, include_in_schema=False),
):
    """获取用户全局 config.yaml (L1) 内容."""
    from harness.config.config_loader import ConfigLoader
    uid = _resolve_user_id(user_id, authorization)
    data = ConfigLoader.load_user_global(uid)
    if data is None:
        return {"exists": False, "message": "用户全局配置不存在 — 请确认已完成注册"}
    return {"exists": True, "config": data}


@router.put("/config/global")
async def update_user_global_config(request: Request):
    """更新用户全局 config.yaml (L1)."""
    from harness.config.config_loader import ConfigLoader, GLOBAL_CONFIG_FILENAME, format_user_global_config_yaml
    from harness.config.paths import get_paths
    body = await request.json()
    auth_header = request.headers.get("Authorization")
    user_id = _resolve_user_id(body.get("user_id"), auth_header)
    config_dir = get_paths().base_dir / "users" / user_id
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / GLOBAL_CONFIG_FILENAME
    config_data = body.get("config", {})
    content = format_user_global_config_yaml(config_data)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "updated", "path": str(config_path)}


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
