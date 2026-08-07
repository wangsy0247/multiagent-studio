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
            unattended=request.unattended,
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
    """Create a new agent (模型由服务器统一配置, 无需指定)."""
    from harness.config.agents_config import (
        AgentConfig, AgentMemoryFields, AgentFeaturesFields,
        AgentLimitsFields, AgentTeamFields,
        save_agent_config, save_agent_soul, save_agent_extensions,
        validate_agent_name,
    )
    body = await request.json()
    name = validate_agent_name(body.get("name", ""))
    soul = body.get("soul", "")

    cfg = AgentConfig(
        name=name,
        display_name=body.get("display_name", name),
        description=body.get("description", ""),
        temperature=body.get("temperature", 0.3),
        max_tokens=body.get("max_tokens", 4096),
        tool_groups=body.get("tool_groups", []),
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
        load_agent_config, save_agent_config, save_agent_soul,
    )
    body = await request.json()
    user_id = body.get("user_id", "default")
    existing = load_agent_config(name, user_id=user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    # 只更新传入的字段 (model 由服务器统一配置, 不接受更新)
    for field in ("display_name", "description", "temperature", "max_tokens",
                  "tool_groups", "skills"):
        if field in body:
            setattr(existing, field, body[field])
    existing.updated_at = ""
    save_agent_config(name, existing, user_id=user_id)
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
    from harness.config.agents_config import delete_agent, is_default_agent
    if is_default_agent(name):
        raise HTTPException(status_code=403, detail="Cannot delete the 'default' agent")
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
# Projects / Tasks API — 已移除 (2026-07-20)
# ---------------------------------------------------------------------------
# 此处原有一套 project/task 镜像端点, 与 app 服务 (app/api/projects.py) 的实现
# 重复且已腐化 (任务存错路径、默认状态非法、项目写旧格式)。前端 /api 全走 app:8000,
# harness 侧端点无人调用, 故删除。团队运行时读写项目/任务请用 harness/team/ 下的
# TeamTaskStore / agent_card 等模块。


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
# System bootstrap status
# ---------------------------------------------------------------------------


@router.get("/system/bootstrap-status")
async def get_bootstrap_status(
    user_id: str = "default",
    harness: HarnessService = Depends(get_harness),
):
    """检查系统配置状态 — 前端在登录后调用, 决定是否显示引导向导."""
    import os as _os
    from harness.config.config_loader import ConfigLoader
    from harness.config.agents_config import list_custom_agents

    issues: list[dict] = []
    ok = True

    # 1. API Key — 由服务器统一提供 (harness/.env env 注入), 不再检查用户配置
    user_config = ConfigLoader.load_user_global(user_id)
    env_api_key = _os.getenv("OPENAI_API_KEY", "")
    if not env_api_key:
        issues.append({
            "field": "api_key",
            "severity": "error",
            "message": "服务器模型 API 未配置 — 请联系管理员在 harness/.env 中配置 OPENAI_API_KEY",
            "fix": "server_env",  # 服务器侧问题, 前端仅展示提示
        })
        ok = False

    # 2. 用户全局配置
    if not user_config:
        issues.append({
            "field": "global_config",
            "severity": "warning",
            "message": "用户全局配置未创建",
            "fix": "auto",  # 系统自动修复
        })
        ok = False

    # 3. Agent
    agents = list_custom_agents(user_id=user_id)
    has_default = any(a.name == "default" for a in agents)

    return {
        "ok": ok,
        "has_default_agent": has_default,
        "agent_count": len(agents),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Tool Groups
# ---------------------------------------------------------------------------


@router.get("/tool-groups")
async def get_tool_groups(
    harness: HarnessService = Depends(get_harness),
):
    """Return the available tool groups."""
    return harness.tool_registry.setup_tool_groups()
