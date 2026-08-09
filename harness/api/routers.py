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


async def _safe_event_stream(event_source, *, thread_id: str):
    """消费事件迭代器并编码为 SSE 帧, 逃逸异常兜底为正常终止的 error 事件.

    响应头发出后无法再改状态码: 若生成器抛出未捕获异常, starlette 只能
    直接断 TCP (无终止 chunk), 下游 httpx 会收到 "incomplete chunked read"
    而看不到真实错误。这里统一捕获, 让客户端总能拿到可解析的终态事件。
    """
    try:
        async for event in event_source:
            yield f"data: {json.dumps(event, default=str)}\n\n"
    except Exception as exc:
        logger.exception("SSE event stream escaped exception (thread=%s)", thread_id)
        # 带完整异常信息 (多行异常只取最后一行会截成无意义片段),
        # 截断防止超长; json.dumps 会把换行转义, SSE 帧仍是单行。
        detail = str(exc).strip().replace("\r", "")[:400]
        error_event = {
            "type": "error",
            "thread_id": thread_id,
            "content": f"执行中断: {detail or '服务内部错误（详见服务端日志）'}",
        }
        yield f"data: {json.dumps(error_event, default=str)}\n\n"


@router.post("/execute")
async def execute(
    request: ExecuteRequest,
    harness: HarnessService = Depends(get_harness),
):
    """Execute an agent task with SSE streaming output."""
    return StreamingResponse(
        _safe_event_stream(
            harness.execute(
                thread_id=request.thread_id,
                user_id=request.user_id,
                message=request.message,
                graph=request.execution_graph,
                files=request.files,
                project_id=request.project_id,
                agent_name=request.agent_name,
                mode=request.mode,
                unattended=request.unattended,
                plan_mode=request.plan_mode,
            ),
            thread_id=request.thread_id,
        ),
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
    return StreamingResponse(
        _safe_event_stream(
            harness.respond_to_clarification(
                thread_id=thread_id,
                answer=request.answer,
            ),
            thread_id=thread_id,
        ),
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
    # per-agent 扩展子集 (extensions_config.yaml 黑名单)
    save_agent_extensions(
        name,
        mcp_servers=body.get("mcp_servers", {}) or {},
        user_id=user_id,
        skills=body.get("skills_enabled", None),
    )
    return {"status": "created", "name": name}


@router.get("/agents/presets")
async def get_preset_agents():
    """Return predefined SubAgent templates.

    必须注册在 /agents/{name} 之前 — FastAPI 按注册顺序匹配,
    否则 "presets" 会被 {name} 吞掉返回 404。
    """
    return PRESET_SUBAGENTS


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

    # per-agent 扩展子集 (extensions_config.yaml): 与现有内容合并后写回
    if "mcp_servers" in body or "skills_enabled" in body:
        from harness.config.agents_config import save_agent_extensions
        from harness.config.config_loader import ConfigLoader
        ext = ConfigLoader.load_agent_extensions(user_id, name) or {}
        mcp = body.get("mcp_servers", ext.get("mcp_servers", {})) or {}
        skl = body.get("skills_enabled", ext.get("skills", {})) or {}
        save_agent_extensions(name, mcp_servers=mcp, user_id=user_id, skills=skl)
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
    thread_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    harness: HarnessService = Depends(get_harness),
):
    """Get token consumption statistics (usage ledger — 含旁路调用与缓存命中拆分)。"""
    from datetime import datetime

    from harness.observability.usage_ledger import get_usage_ledger

    def _to_ts(date_str: str | None, *, end_of_day: bool = False) -> float | None:
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str)
        except ValueError:
            return None
        if end_of_day and len(date_str) == 10:  # 纯日期 → 当天末尾
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.timestamp()

    agg = get_usage_ledger().aggregate(
        user_id or "default",
        thread_id=thread_id,
        start_ts=_to_ts(start_date),
        end_ts=_to_ts(end_date, end_of_day=True),
    )
    return {
        # 兼容旧响应形状 (admin 页 / TokenChart 在用)
        "total_prompt_tokens": agg["prompt_tokens"],
        "total_completion_tokens": agg["completion_tokens"],
        "total_tokens": agg["total_tokens"],
        "total_cost_usd": 0,
        # 新字段
        "prompt_tokens": agg["prompt_tokens"],
        "completion_tokens": agg["completion_tokens"],
        "cache_hit_tokens": agg["cache_hit_tokens"],
        "cache_miss_tokens": agg["cache_miss_tokens"],
        "by_model": {
            r["model"]: {
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "total_tokens": r["total_tokens"],
                "cost_usd": 0,
            }
            for r in agg["by_model"]
        },
        "by_date": [
            {"date": r["date"], "tokens": r["total_tokens"], "cost": 0}
            for r in agg["by_date"]
        ],
        "by_source": agg["by_source"],
    }


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
