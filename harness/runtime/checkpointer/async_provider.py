"""Async checkpointer provider — factory with graceful degradation.

DeerFlow-aligned: creates the appropriate ``BaseCheckpointSaver`` for the
configured backend.  SQLite and Postgres savers are imported at runtime so
that neither is a hard dependency — if the import fails the provider falls
back to ``MemorySaver`` (which is always available).

langgraph-checkpoint >= 3.x: ``from_conn_string`` is an ``@asynccontextmanager``
that yields the saver — the context manager must stay alive for the lifetime
of the saver.  This provider enters the context manager via ``__aenter__`` and
stores it so ``close()`` can call ``__aexit__``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from harness.config.checkpointer_config import CheckpointerConfig, CheckpointerBackend
from harness.models import (
    ClarificationRequest,
    SubAgentResult,
    TodoItem,
    TokenUsage,
)

logger = logging.getLogger(__name__)

# Harness Pydantic models that may appear directly in HarnessState checkpoints.
HARNESS_MSGPACK_TYPES = [
    ClarificationRequest,
    SubAgentResult,
    TodoItem,
    TokenUsage,
]


def _create_harness_serde() -> JsonPlusSerializer:
    """Serializer that explicitly allows harness custom state types."""
    return JsonPlusSerializer(allowed_msgpack_modules=HARNESS_MSGPACK_TYPES)

# ---------------------------------------------------------------------------
# human-readable error messages for missing packages
# ---------------------------------------------------------------------------

SQLITE_INSTALL = (
    "langgraph-checkpoint-sqlite is not installed. "
    "Install it with: pip install langgraph-checkpoint-sqlite"
)
POSTGRES_INSTALL = (
    "langgraph-checkpoint-postgres is not installed. "
    "Install it with: pip install langgraph-checkpoint-postgres"
)

# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


class AsyncCheckpointerProvider:
    """Creates and manages the lifecycle of an async LangGraph checkpointer.

    Parameters
    ----------
    config : CheckpointerConfig
        Backend selection and connection parameters.
    """

    def __init__(self, config: CheckpointerConfig) -> None:
        self.config = config
        self._saver: BaseCheckpointSaver | None = None
        # 手动管理的 aiosqlite / asyncpg 连接，在 close() 时关闭
        self._conn: object | None = None

    # ------------------------------------------------------------------
    # factory
    # ------------------------------------------------------------------

    async def get_checkpointer(self) -> BaseCheckpointSaver:
        """Return an initialized checkpointer for the configured backend.

        On failure (missing package, connection error, …) the provider
        degrades gracefully to ``MemorySaver`` and logs a warning so the
        service stays operational.

        This method is idempotent — repeated calls return the existing saver.
        """
        if self._saver is not None:
            return self._saver

        backend: CheckpointerBackend = self.config.backend

        try:
            if backend == "memory":
                self._saver = self._create_memory()
            elif backend == "sqlite":
                self._saver = await self._create_sqlite()
            elif backend == "postgres":
                self._saver = await self._create_postgres()
            else:
                raise ValueError(f"Unknown checkpointer backend: {backend}")
        except Exception:
            logger.exception(
                "Failed to create checkpointer backend=%r; refusing silent fallback",
                backend,
            )
            raise

        logger.info(
            "Checkpointer ready: backend=%s type=%s",
            backend,
            type(self._saver).__name__,
        )
        return self._saver

    # ------------------------------------------------------------------
    # backend factories
    # ------------------------------------------------------------------

    @staticmethod
    def _create_memory() -> MemorySaver:
        """In-process checkpointer — no persistence, always available."""
        return MemorySaver(serde=_create_harness_serde())

    async def _create_sqlite(self) -> BaseCheckpointSaver:
        """File-based async SQLite checkpointer."""
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError:
            logger.error(SQLITE_INSTALL)
            raise ImportError(SQLITE_INSTALL)

        from harness.config.checkpointer_config import get_checkpointer_config
        from harness.config.paths import get_paths

        cfg = get_checkpointer_config()
        db_dir = Path(cfg.sqlite_dir) if cfg.sqlite_dir else get_paths().data_dir
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "deerflow.db"

        # langgraph-checkpoint >= 3.x: from_conn_string 是 async context
        # manager，但 LangGraph 内部会自动调用 saver.setup()，后者会再次
        # await conn 导致 aiosqlite 线程重复启动 → RuntimeError。
        # 绕过 from_conn_string，手动创建连接和 saver，调用一次 setup()
        # 设置 is_setup=True，之后 LangGraph 内部调用 setup() 就是空操作。
        conn = await aiosqlite.connect(str(db_path))
        self._conn = conn  # 保存以便 close() 时关闭
        saver = AsyncSqliteSaver(conn, serde=_create_harness_serde())
        await saver.setup()
        logger.info("AsyncSqliteSaver initialized at %s", db_path)
        return saver

    async def _create_postgres(self) -> BaseCheckpointSaver:
        """Database-backed async Postgres checkpointer."""
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError:
            logger.error(POSTGRES_INSTALL)
            raise ImportError(POSTGRES_INSTALL)

        from harness.config.checkpointer_config import get_checkpointer_config

        cfg = get_checkpointer_config()
        if not cfg.postgres_url:
            raise ValueError(
                "postgres backend selected but postgres_url is empty"
            )

        # 与 SQLite 同理：绕过 from_conn_string，手动创建连接和 saver
        import asyncpg
        conn = await asyncpg.connect(cfg.postgres_url)
        self._conn = conn
        saver = AsyncPostgresSaver(conn, serde=_create_harness_serde())
        await saver.setup()
        logger.info("AsyncPostgresSaver initialized")
        return saver

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release resources held by the checkpointer (if any)."""
        # 关闭手动管理的数据库连接
        if self._conn is not None:
            conn_close = getattr(self._conn, "close", None)
            if conn_close is not None:
                try:
                    await conn_close()
                except Exception:
                    logger.warning("Error closing DB connection", exc_info=True)
            self._conn = None

        if self._saver is None:
            return

        # Fallback: call aclose() if available (MemorySaver doesn't have one).
        closer = getattr(self._saver, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                logger.warning("Error closing checkpointer", exc_info=True)
        self._saver = None
