"""扩展管理 API — MCP server 与 skill 的鉴权代理 (转发到 Harness).

Harness 侧接口无鉴权, 只允许经本服务 (app:8000, JWT) 暴露给前端。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_current_user
from app.models.user import User
from app.services.harness_client import (
    HarnessUnavailableError,
    get_harness_client,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["扩展管理"])


async def _proxy(method: str, path: str, **kwargs):
    harness = get_harness_client()
    try:
        resp = await harness._request(method, path, timeout=30, **kwargs)
    except HarnessUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if resp.status_code >= 400:
        # 透传 harness 的错误详情 (404/400/409 等)
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


# ── MCP servers (全局, 服务器级) ─────────────────────────────────────────


@router.get("/mcp/servers")
async def list_mcp_servers(current_user: User = Depends(get_current_user)):
    return await _proxy("GET", "/api/mcp/servers")


@router.put("/mcp/servers/{name}")
async def upsert_mcp_server(
    name: str, request: Request, current_user: User = Depends(get_current_user)
):
    body = await request.json()
    return await _proxy("PUT", f"/api/mcp/servers/{name}", json=body)


@router.put("/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(
    name: str, request: Request, current_user: User = Depends(get_current_user)
):
    body = await request.json()
    return await _proxy("PUT", f"/api/mcp/servers/{name}/enabled", json=body)


@router.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str, current_user: User = Depends(get_current_user)):
    return await _proxy("DELETE", f"/api/mcp/servers/{name}")


# ── Skills (builtin 全局启停 + 用户私有 CRUD/安装) ────────────────────────


@router.get("/skills")
async def list_skills(current_user: User = Depends(get_current_user)):
    uid = current_user.username
    return await _proxy("GET", f"/api/skills?user_id={uid}")


@router.get("/skills/agent-skills")
async def list_agent_skills(current_user: User = Depends(get_current_user)):
    """按 agent 聚合的成员私有进化技能 (probation/active)."""
    uid = current_user.username
    return await _proxy("GET", f"/api/skills/agent-skills?user_id={uid}")


@router.put("/skills/{name}/enabled")
async def toggle_skill(
    name: str, request: Request, current_user: User = Depends(get_current_user)
):
    body = await request.json()
    return await _proxy("PUT", f"/api/skills/{name}", json=body)


@router.get("/skills/custom/{name}")
async def get_custom_skill(name: str, current_user: User = Depends(get_current_user)):
    uid = current_user.username
    return await _proxy("GET", f"/api/skills/custom/{name}?user_id={uid}")


@router.put("/skills/custom/{name}")
async def write_custom_skill(
    name: str, request: Request, current_user: User = Depends(get_current_user)
):
    uid = current_user.username
    body = await request.json()
    return await _proxy("PUT", f"/api/skills/custom/{name}?user_id={uid}", json=body)


@router.delete("/skills/custom/{name}")
async def delete_custom_skill(name: str, current_user: User = Depends(get_current_user)):
    uid = current_user.username
    return await _proxy("DELETE", f"/api/skills/custom/{name}?user_id={uid}")


@router.post("/skills/install")
async def install_skill(request: Request, current_user: User = Depends(get_current_user)):
    uid = current_user.username
    body = await request.json()
    body["user_id"] = uid  # 用户身份以 JWT 为准, 忽略客户端传入
    return await _proxy("POST", "/api/skills/install", json=body)
