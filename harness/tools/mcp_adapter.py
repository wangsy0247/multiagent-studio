"""MCP tool adapter with a graceful fallback when packages are unavailable."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# ── keep MCP sessions alive across the application lifetime ──
_mcp_sessions: list[tuple[Any, Any, Any]] = []


async def load_mcp_tools_from_config(config_path: str | Path) -> list[BaseTool]:
    """Load tools from an MCP server configuration file.

    The file is expected to follow the ``mcpServers`` format used by MCP.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        logger.warning("MCP config not found: %s", config_path)
        return []

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse MCP config: %s", exc)
        return []

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from langchain_mcp_adapters.tools import load_mcp_tools
    except Exception as exc:
        logger.warning("MCP packages not available: %s", exc)
        return []

    loaded: list[BaseTool] = []
    for server_name, server_config in data.get("mcpServers", {}).items():
        # ── 修复 #6: 尊重 enabled 标志 ──
        if not server_config.get("enabled", False):
            logger.info("MCP server '%s' is disabled, skipping", server_name)
            continue

        try:
            # ── 修复 #7: 展开环境变量 ──
            raw_env = server_config.get("env") or {}
            expanded_env = {
                k: os.path.expandvars(v) for k, v in raw_env.items()
            } if raw_env else None

            params = StdioServerParameters(
                command=server_config["command"],
                args=server_config.get("args", []),
                env=expanded_env,
            )
            # ── 修复 #3: 保持会话存活（非 async with） ──
            read, write = await stdio_client(params).__aenter__()
            session = await ClientSession(read, write).__aenter__()
            await session.initialize()
            tools = await load_mcp_tools(session)
            _mcp_sessions.append((read, write, session))
            for tool in tools:
                loaded.append(tool)
            logger.info("Loaded %d tools from MCP server '%s'", len(tools), server_name)
        except Exception as exc:
            logger.warning("Failed to load MCP server '%s': %s", server_name, exc)
    return loaded
