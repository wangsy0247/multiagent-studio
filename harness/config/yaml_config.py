"""YAML configuration types — database config used by persistence layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatabaseConfig:
    """Configuration for the application database (Runs, Threads, Events).

    Separate from the LangGraph *checkpointer* config, though both default
    to the same ``deerflow.db`` file for single-node SQLite deployments.
    """

    section = "database"
    backend: str = "sqlite"  # memory | sqlite | postgres
    sqlite_dir: str = ""     # empty → resolved via Paths.data_dir
    postgres_url: str = ""
