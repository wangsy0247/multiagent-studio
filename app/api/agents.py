"""Agent 管理 API — per-user persistent agents (SOUL.md + config.yaml)."""

import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.engine import get_db
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent管理"])

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _validate_name(name: str) -> str:
    """agent 名会拼接进文件路径, 只放行安全字符."""
    if not name or not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid agent name: {name!r}")
    return name


@router.get("")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from harness.config.agents_config import list_custom_agents
    uid = current_user.username
    agents = list_custom_agents(user_id=uid)
    return {"agents": [a.model_dump() for a in agents], "count": len(agents)}


@router.post("")
async def create_agent(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
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
            max_facts=mem_data.get("max_facts", 10),
            injection_enabled=mem_data.get("injection_enabled", True),
            max_injection_tokens=mem_data.get("max_injection_tokens", 500),
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
            can_delegate=body.get("can_delegate", team_data.get("can_delegate", True)),
            memory_scope=body.get("memory_scope", team_data.get("memory_scope", "agent")),
        ),
        subagents=AgentSubagentsFields(
            timeout_seconds=sub_data.get("timeout_seconds", 900),
            max_concurrent=sub_data.get("max_concurrent", 3),
        ),
    )
    user_id = current_user.username
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from harness.config.agents_config import load_agent_config, load_agent_soul
    _validate_name(name)
    uid = current_user.username
    cfg = load_agent_config(name, user_id=uid)
    if cfg is None: raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"agent": cfg.model_dump(), "soul": load_agent_soul(name, user_id=uid)}


@router.put("/{name}")
async def update_agent(name: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from harness.config.agents_config import (
        AgentConfig, AgentMemoryFields, AgentFeaturesFields,
        AgentLimitsFields, AgentTeamFields, AgentSubagentsFields,
        load_agent_config, save_agent_config, save_agent_soul,
    )
    body = await request.json()
    _validate_name(name)
    user_id = current_user.username
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
            max_facts=mem_data.get("max_facts", existing.memory.max_facts),
            injection_enabled=mem_data.get("injection_enabled", existing.memory.injection_enabled),
            max_injection_tokens=mem_data.get("max_injection_tokens", existing.memory.max_injection_tokens),
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from harness.config.agents_config import delete_agent, is_default_agent
    _validate_name(name)
    if is_default_agent(name):
        raise HTTPException(status_code=403, detail="Cannot delete the 'default' agent — it is required by the system")
    uid = current_user.username
    if not delete_agent(name, user_id=uid):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return {"status": "deleted", "name": name}


# ── 用户全局配置 ──
@router.get("/config/global")
async def get_user_global_config(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取用户全局 config.yaml (L1) 内容."""
    from harness.config.config_loader import ConfigLoader
    uid = current_user.username
    data = ConfigLoader.load_user_global(uid)
    if data is None:
        return {"exists": False, "message": "用户全局配置不存在 — 请确认已完成注册"}
    return {"exists": True, "config": data}


@router.put("/config/global")
async def update_user_global_config(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """更新用户全局 config.yaml (L1)."""
    from harness.config.config_loader import ConfigLoader, GLOBAL_CONFIG_FILENAME, format_user_global_config_yaml
    from harness.config.paths import get_paths
    body = await request.json()
    user_id = current_user.username
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from harness.memory.updater import get_memory_data
    _validate_name(name)
    uid = current_user.username
    return {"name": name, "memory": get_memory_data(agent_name=name, user_id=uid)}


@router.delete("/{name}/memory")
async def clear_agent_memory(
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from harness.memory.updater import clear_memory_data
    _validate_name(name)
    uid = current_user.username
    clear_memory_data(agent_name=name, user_id=uid)
    return {"status": "cleared", "name": name}
