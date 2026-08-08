"""MCP server 管理 API — extensions_config.json 的 mcpServers 段 CRUD.

写入走原子写 + mtime 热更新 (cache.py 每次执行前检查), 写后主动 reset
缓存立即生效。MCP server 是全局配置 (服务器级), per-agent 子集在
agent 的 extensions_config.yaml 中管理 (agents API)。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["MCP 管理"])

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _validate_name(name: str) -> str:
    if not _SAFE_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail=f"Invalid server name: {name!r}")
    return name


class McpServerUpsertRequest(BaseModel):
    """新增/覆盖一个 MCP server (字段与 McpServerConfig 对齐)."""

    enabled: bool = True
    type: str = Field(default="stdio", pattern="^(stdio|sse|http)$")
    # stdio
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http / sse
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""


class McpServerEnabledRequest(BaseModel):
    enabled: bool


def _read_config() -> dict[str, Any]:
    from harness.api.routers_skills import _read_extensions_config

    return _read_extensions_config()


def _write_config(config: dict[str, Any]) -> None:
    from harness.api.routers_skills import _write_extensions_config

    _write_extensions_config(config)
    # 主动失效 MCP 工具缓存 + session 池, 不等下一次执行的 mtime 检查
    try:
        from harness.mcp_integration.cache import reset_mcp_tools_cache

        reset_mcp_tools_cache()
    except Exception:
        logger.warning("Failed to reset MCP tools cache", exc_info=True)


def _validate_server_cfg(body: McpServerUpsertRequest) -> None:
    if body.type == "stdio" and not body.command:
        raise HTTPException(status_code=400, detail="stdio 类型必须提供 command")
    if body.type in ("http", "sse") and not body.url:
        raise HTTPException(status_code=400, detail=f"{body.type} 类型必须提供 url")


@router.get("/servers")
async def list_mcp_servers() -> dict[str, Any]:
    """列出所有 MCP server 及其配置."""
    config = _read_config()
    servers = config.get("mcpServers", {}) or {}
    return {"servers": servers, "count": len(servers)}


@router.put("/servers/{name}")
async def upsert_mcp_server(name: str, body: McpServerUpsertRequest) -> dict[str, Any]:
    """新增或覆盖一个 MCP server."""
    _validate_name(name)
    _validate_server_cfg(body)
    config = _read_config()
    servers = config.setdefault("mcpServers", {})
    existed = name in servers
    entry: dict[str, Any] = {
        "enabled": body.enabled,
        "type": body.type,
        "description": body.description,
    }
    if body.type == "stdio":
        entry["command"] = body.command
        entry["args"] = body.args
        entry["env"] = body.env
    else:
        entry["url"] = body.url
        entry["headers"] = body.headers
    servers[name] = entry
    _write_config(config)
    logger.info("MCP server '%s' %s via REST API", name, "updated" if existed else "created")
    return {"status": "updated" if existed else "created", "name": name}


@router.put("/servers/{name}/enabled")
async def set_mcp_server_enabled(name: str, body: McpServerEnabledRequest) -> dict[str, Any]:
    """快速启停一个 MCP server."""
    _validate_name(name)
    config = _read_config()
    servers = config.get("mcpServers", {})
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    servers[name]["enabled"] = body.enabled
    _write_config(config)
    logger.info("MCP server '%s' %s via REST API", name, "enabled" if body.enabled else "disabled")
    return {"status": "ok", "name": name, "enabled": body.enabled}


@router.delete("/servers/{name}")
async def delete_mcp_server(name: str) -> dict[str, Any]:
    """删除一个 MCP server."""
    _validate_name(name)
    config = _read_config()
    servers = config.get("mcpServers", {})
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    del servers[name]
    _write_config(config)
    logger.info("MCP server '%s' deleted via REST API", name)
    return {"status": "deleted", "name": name}
