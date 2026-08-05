"""Checkpointer configuration — harness-aligned singleton pattern.

Supports three backends:

* ``memory`` — in-process MemorySaver (no persistence, no extra deps)
* ``sqlite`` — ``langgraph-checkpoint-sqlite`` (file-based persistence)
* ``postgres`` — ``langgraph-checkpoint-postgres`` (database persistence)

Configuration is loaded from YAML (``checkpointer`` section) and managed as a
module-level singleton so every component reads the same config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

CheckpointerBackend = Literal["memory", "sqlite", "postgres"]


@dataclass
class CheckpointerConfig:
    """Checkpointer configuration.

    Attributes:
        backend: One of ``memory``, ``sqlite``, ``postgres``.
        sqlite_dir: Directory for the SQLite database file (sqlite only).
        postgres_url: Connection string for Postgres (postgres only).
    """

    backend: CheckpointerBackend = "memory"
    sqlite_dir: str = ""  # empty → resolved via Paths.data_dir at runtime
    postgres_url: str = ""


# ---------------------------------------------------------------------------
# module-level singleton (mirrors the standard's global config pattern)
# ---------------------------------------------------------------------------

_checkpointer_config: CheckpointerConfig | None = None


def get_checkpointer_config() -> CheckpointerConfig:
    """Return the current checkpointer config, falling back to defaults."""
    global _checkpointer_config
    if _checkpointer_config is None:
        _checkpointer_config = CheckpointerConfig()
        logger.info("CheckpointerConfig defaulted to backend='memory'")
    return _checkpointer_config


def set_checkpointer_config(config: CheckpointerConfig) -> None:
    """Replace the singleton config (e.g. for testing)."""
    global _checkpointer_config
    _checkpointer_config = config
    logger.info("CheckpointerConfig set to backend=%s", config.backend)


def load_checkpointer_config_from_dict(data: dict | None) -> CheckpointerConfig:
    """Load checkpointer configuration from a YAML-parsed dictionary.

    Called by ``HarnessService`` during initialization.  If *data* is
    ``None`` or empty the default ``memory`` backend is used.
    """
    global _checkpointer_config
    if not data:
        cfg = CheckpointerConfig()
    else:
        backend_raw = data.get("backend", "memory")
        if backend_raw not in ("memory", "sqlite", "postgres"):
            logger.warning(
                "Unknown checkpointer backend=%r, falling back to 'memory'",
                backend_raw,
            )
            backend_raw = "memory"
        cfg = CheckpointerConfig(
            backend=backend_raw,  # type: ignore[arg-type]
            sqlite_dir=data.get("sqlite_dir", ""),
            postgres_url=data.get("postgres_url", ""),
        )
    _checkpointer_config = cfg
    logger.info("CheckpointerConfig loaded: backend=%s", cfg.backend)
    return cfg
