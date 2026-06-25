"""Async database engine lifecycle — SQLite WAL + Postgres support.

DeerFlow-aligned: creates a single ``deerflow.db`` shared by LangGraph's
checkpointer and the application-layer ORM tables.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harness.config.paths import get_paths
from harness.config.yaml_config import DatabaseConfig
from harness.persistence.base import Base

logger = logging.getLogger(__name__)

# Import models so they register with Base.metadata
import harness.persistence.models  # noqa: F401


class DatabaseEngine:
    """Manage the async SQLAlchemy engine and session factory.

    Parameters
    ----------
    config : DatabaseConfig
        Backend selection and connection parameters.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        self.engine = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

        if config.backend == "memory":
            logger.info("DatabaseEngine: memory backend — no persistence")
            return

        if config.backend == "sqlite":
            db_dir = (
                Path(config.sqlite_dir)
                if config.sqlite_dir
                else get_paths().data_dir
            )
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "deerflow.db"
            self._url = f"sqlite+aiosqlite:///{db_path}"
            logger.info("DatabaseEngine: sqlite → %s", db_path)
        elif config.backend == "postgres":
            if not config.postgres_url:
                raise ValueError(
                    "postgres backend requires postgres_url"
                )
            self._url = config.postgres_url
            logger.info("DatabaseEngine: postgres")
        else:
            raise ValueError(f"Unknown backend: {config.backend}")

        self.engine = create_async_engine(
            self._url,
            echo=False,
            json_serializer=lambda obj: __import__("json").dumps(
                obj, ensure_ascii=False
            ),
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        # SQLite WAL pragma for concurrent read/write
        if config.backend == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    async def init_tables(self) -> None:
        """Create all ORM tables if they don't exist (idempotent)."""
        if self.engine is None:
            return
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("DatabaseEngine: tables initialised")

    async def close(self) -> None:
        """Dispose the engine and release connections."""
        if self.engine is not None:
            await self.engine.dispose()
            logger.info("DatabaseEngine: closed")
