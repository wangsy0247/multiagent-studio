"""Persistent MCP session pool for stateful tool calls.

When MCP tools are loaded via langchain-mcp-adapters with ``session=None``,
each tool call creates a new MCP session. For stateful servers like Playwright,
this means browser state (opened pages, filled forms) is lost between calls.

This module provides a session pool that maintains persistent MCP sessions,
scoped by ``(server_name, scope_key)`` — typically scope_key is the thread_id —
so that consecutive tool calls share the same session and server-side state.
Sessions are evicted in LRU order when the pool reaches capacity.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp import ClientSession

logger = logging.getLogger(__name__)


class MCPSessionPool:
    """Manages persistent MCP sessions scoped by ``(server_name, scope_key)``."""

    MAX_SESSIONS = 256
    SESSION_CLOSE_TIMEOUT = 5.0

    def __init__(self) -> None:
        self._entries: OrderedDict[
            tuple[str, str],
            tuple[ClientSession, asyncio.AbstractEventLoop, str | None],
        ] = OrderedDict()
        self._context_managers: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        # 并发创建竞态: Phase2 (建 session) 在锁外, 两个并发首调会各建一个、
        # 被覆盖者的子进程/连接泄漏 → 创建路径用 per-loop asyncio 锁串行化
        self._create_lock: asyncio.Lock | None = None
        self._create_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_create_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._create_lock is None or self._create_lock_loop is not loop:
                self._create_lock = asyncio.Lock()
                self._create_lock_loop = loop
        return self._create_lock

    @staticmethod
    def _merge_connection(
        connection: dict[str, Any], headers: dict[str, str] | None
    ) -> dict[str, Any]:
        """把 OAuth 等动态 headers 合并进连接配置 (仅 sse/http 传输)."""
        if not headers:
            return connection
        if connection.get("transport") not in ("sse", "http"):
            return connection
        merged = dict(connection)
        merged["headers"] = {**connection.get("headers", {}), **headers}
        return merged

    async def get_session(
        self,
        server_name: str,
        scope_key: str,
        connection: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> ClientSession:
        """Get or create a persistent MCP session.

        If an existing session was created in a different event loop, it is
        closed and replaced with a fresh one in the current loop.

        ``headers`` (如 OAuth 刷新后的 Authorization) 与已建 session 的
        创建时 headers 不一致时, 就地重建 session — 否则刷新出的新 token
        永远不会生效, 初始 token 过期后所有调用持续 401 到进程重启。
        """
        key = (server_name, scope_key)
        current_loop = asyncio.get_running_loop()
        auth = (headers or {}).get("Authorization")

        # 快速路径: 无锁命中 (同 loop 且 headers 一致)
        with self._lock:
            if key in self._entries:
                session, loop, entry_auth = self._entries[key]
                if loop is current_loop and entry_auth == auth:
                    self._entries.move_to_end(key)
                    return session

        # 慢速路径: per-loop 创建锁串行化 (防并发双建泄漏)
        async with self._get_create_lock():
            # Phase 1: inspect/mutate the registry under the thread lock (no awaits).
            cms_to_close: list[tuple[tuple[str, str], Any]] = []
            with self._lock:
                if key in self._entries:
                    session, loop, entry_auth = self._entries[key]
                    if loop is current_loop and entry_auth == auth:
                        self._entries.move_to_end(key)
                        return session
                    # loop 变了或 headers 变了 – 就地重建
                    cm = self._context_managers.pop(key, None)
                    self._entries.pop(key)
                    if cm is not None:
                        cms_to_close.append((key, cm))

                # Evict LRU entries when at capacity.
                while len(self._entries) >= self.MAX_SESSIONS:
                    oldest_key = next(iter(self._entries))
                    cm = self._context_managers.pop(oldest_key, None)
                    self._entries.pop(oldest_key)
                    if cm is not None:
                        cms_to_close.append((oldest_key, cm))

            # Phase 2: async cleanup outside the lock.
            for close_key, cm in cms_to_close:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "Error closing MCP session %s", close_key, exc_info=True
                    )

            from langchain_mcp_adapters.sessions import create_session

            cm = create_session(self._merge_connection(connection, headers))
            try:
                session = await cm.__aenter__()
                await session.initialize()
            except Exception:
                # initialize 失败必须 aexit, 否则 stdio 子进程泄漏
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "Error closing failed MCP session %s", key, exc_info=True
                    )
                raise

            # Phase 3: register the new session under the lock.
            with self._lock:
                self._entries[key] = (session, current_loop, auth)
                self._context_managers[key] = cm
            logger.info(
                "Created persistent MCP session for %s/%s", server_name, scope_key
            )
            return session

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    async def _close_cm(self, key: tuple[str, str], cm: Any) -> None:
        """Close a single context manager (must be called WITHOUT the lock)."""
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            logger.warning(
                "Error closing MCP session %s", key, exc_info=True
            )

    async def close_scope(self, scope_key: str) -> None:
        """Close all sessions for a given scope (e.g. thread_id)."""
        with self._lock:
            keys = [k for k in self._entries if k[1] == scope_key]
            cms = [(k, self._context_managers.pop(k, None)) for k in keys]
            for k in keys:
                self._entries.pop(k, None)
        for key, cm in cms:
            if cm is not None:
                await self._close_cm(key, cm)

    async def close_server(self, server_name: str) -> None:
        """Close all sessions for a given server."""
        with self._lock:
            keys = [k for k in self._entries if k[0] == server_name]
            cms = [(k, self._context_managers.pop(k, None)) for k in keys]
            for k in keys:
                self._entries.pop(k, None)
        for key, cm in cms:
            if cm is not None:
                await self._close_cm(key, cm)

    async def close_all(self) -> None:
        """Close every managed session."""
        with self._lock:
            cms = list(self._context_managers.items())
            self._context_managers.clear()
            self._entries.clear()
        for key, cm in cms:
            await self._close_cm(key, cm)

    def close_all_sync(self) -> None:
        """Close all sessions using their owning event loops (synchronous).

        Safe to call from any thread without an active event loop.
        """
        with self._lock:
            entries = list(self._entries.items())
            cms = dict(self._context_managers)
            self._entries.clear()
            self._context_managers.clear()

        for key, (_, loop, _auth) in entries:
            cm = cms.get(key)
            if cm is None or loop.is_closed():
                continue
            try:
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        cm.__aexit__(None, None, None), loop
                    )
                    future.result(timeout=self.SESSION_CLOSE_TIMEOUT)
                else:
                    loop.run_until_complete(cm.__aexit__(None, None, None))
            except Exception:
                logger.debug(
                    "Error closing MCP session %s during sync close",
                    key,
                    exc_info=True,
                )


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_pool: MCPSessionPool | None = None
_pool_lock = threading.Lock()


def get_session_pool() -> MCPSessionPool:
    """Return the global session-pool singleton."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = MCPSessionPool()
    return _pool


def reset_session_pool() -> None:
    """Reset the singleton (for tests)."""
    global _pool
    _pool = None
