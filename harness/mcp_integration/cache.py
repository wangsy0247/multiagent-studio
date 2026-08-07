"""Cache for MCP tools to avoid repeated loading.

Automatically invalidates when ``extensions_config.json`` is modified (mtime check).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized = False
_config_mtime: float | None = None

# 初始化锁按事件循环惰性重建 — 模块级 asyncio.Lock 在跨 loop 复用时
# (get_cached_mcp_tools 的 ThreadPoolExecutor asyncio.run 路径) 会抛
# "attached to a different loop"
_init_lock_guard = threading.Lock()
_initialization_lock: asyncio.Lock | None = None
_init_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_initialization_lock() -> asyncio.Lock:
    global _initialization_lock, _init_lock_loop
    loop = asyncio.get_running_loop()
    with _init_lock_guard:
        if _initialization_lock is None or _init_lock_loop is not loop:
            _initialization_lock = asyncio.Lock()
            _init_lock_loop = loop
    return _initialization_lock


def _get_config_mtime() -> float | None:
    """Get the modification time of the extensions config file."""
    from harness.config.extensions_config import ExtensionsConfig

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path and config_path.exists():
        return os.path.getmtime(config_path)
    return None


def _is_cache_stale() -> bool:
    """Check if the cache is stale due to config file changes."""
    global _config_mtime

    if not _cache_initialized:
        return False

    current_mtime = _get_config_mtime()

    if _config_mtime is None or current_mtime is None:
        return False

    if current_mtime > _config_mtime:
        logger.info(
            "MCP config file has been modified (mtime: %s -> %s), cache is stale",
            _config_mtime,
            current_mtime,
        )
        return True

    return False


async def initialize_mcp_tools(config_path: str = "") -> list[BaseTool]:
    """Initialize and cache MCP tools.

    This should be called once at application startup.

    Args:
        config_path: Path to ``extensions_config.json``. Empty = auto-detect.

    Returns:
        List of LangChain tools from all enabled MCP servers.
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime

    async with _get_initialization_lock():
        if _cache_initialized:
            logger.info("MCP tools already initialized")
            return _mcp_tools_cache or []

        from harness.mcp_integration.tools import get_mcp_tools

        logger.info("Initializing MCP tools...")
        _mcp_tools_cache = await get_mcp_tools(config_path=config_path)
        _cache_initialized = True
        _config_mtime = _get_config_mtime()
        logger.info(
            "MCP tools initialized: %d tool(s) loaded (config mtime: %s)",
            len(_mcp_tools_cache),
            _config_mtime,
        )

        # tool_search 延迟加载: 按 config.yaml 的 tool_search 段决定是否
        # 将 MCP 工具注册为 deferred (目录重建 → hash 变更 → 旧 promoted 失效)
        try:
            from harness.tools.tool_search import configure_deferred_tools

            configure_deferred_tools(_mcp_tools_cache)
        except Exception:
            logger.warning("Failed to configure deferred tools", exc_info=True)

        return _mcp_tools_cache


def get_cached_mcp_tools() -> list[BaseTool]:
    """Get cached MCP tools with lazy initialization.

    If tools are not initialized, automatically initializes them.
    Also checks if the config file has been modified since last initialization,
    and re-initializes if needed.
    """
    global _cache_initialized

    if _is_cache_stale():
        logger.info("MCP cache is stale, resetting for re-initialization...")
        reset_mcp_tools_cache()

    if not _cache_initialized:
        logger.info("MCP tools not initialized, performing lazy initialization...")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run, initialize_mcp_tools()
                    )
                    future.result()
            else:
                loop.run_until_complete(initialize_mcp_tools())
        except RuntimeError:
            try:
                asyncio.run(initialize_mcp_tools())
            except Exception:
                logger.exception("Failed to lazy-initialize MCP tools")
                return []
        except Exception:
            logger.exception("Failed to lazy-initialize MCP tools")
            return []

    return _mcp_tools_cache or []


def reset_mcp_tools_cache() -> None:
    """Reset the MCP tools cache.

    Also closes all persistent MCP sessions.
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None

    try:
        from harness.mcp_integration.session_pool import get_session_pool

        pool = get_session_pool()
        pool.close_all_sync()
    except Exception:
        logger.debug(
            "Could not close MCP session pool on cache reset", exc_info=True
        )

    from harness.mcp_integration.session_pool import reset_session_pool

    reset_session_pool()

    # 同步清空 tool_search 的 deferred 设置, 避免重建前用到过期目录
    try:
        from harness.tools.tool_search import configure_deferred_tools

        configure_deferred_tools([])
    except Exception:
        logger.debug("Could not clear deferred tool setup on cache reset", exc_info=True)

    logger.info("MCP tools cache reset")
