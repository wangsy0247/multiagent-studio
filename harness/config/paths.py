"""Centralised path management -- DeerFlow-aligned.

All runtime data is rooted under ``~/.multiagent-studio/`` (configurable
via ``HARNESS_DATA_ROOT``).  Every path method validates *user_id* and
*thread_id* against a safe-id regex to prevent directory traversal.

Usage::

    from harness.config.paths import get_paths
    paths = get_paths()
    mem_file = paths.user_memory_file("alice")
    work_dir = paths.sandbox_work_dir("thread-1", "alice")
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_user_id(user_id: str) -> str:
    if not _ID_RE.match(user_id):
        raise ValueError(
            f"Invalid user_id={user_id!r}.  Must match {_ID_RE.pattern}"
        )
    return user_id


def _validate_thread_id(thread_id: str) -> str:
    if not _ID_RE.match(thread_id):
        raise ValueError(
            f"Invalid thread_id={thread_id!r}.  Must match {_ID_RE.pattern}"
        )
    return thread_id


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class Paths:
    """Centralised path resolver for all harness runtime data."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is not None:
            self._base = Path(base_dir).expanduser().resolve()
        else:
            from harness.config import load_config

            self._base = Path(load_config().data_root)
        logger.debug("Paths base_dir=%s", self._base)

    # -- top-level -----------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        """Root of all harness data (``~/.multiagent-studio``)."""
        return self._base

    @property
    def data_dir(self) -> Path:
        """SQLite database directory (``{base}/data``)."""
        return self._base / "data"

    @property
    def database_file(self) -> Path:
        """Shared SQLite database (``{data_dir}/deerflow.db``)."""
        return self.data_dir / "deerflow.db"

    # -- users ---------------------------------------------------------------

    def user_dir(self, user_id: str) -> Path:
        """Per-user directory (``{base}/users/{uid}``)."""
        return self._base / "users" / _validate_user_id(user_id)

    def user_memory_file(self, user_id: str) -> Path:
        """Per-user long-term memory (``{user_dir}/memory.json``)."""
        return self.user_dir(user_id) / "memory.json"

    def user_agents_dir(self, user_id: str) -> Path:
        """Per-user custom agents directory (``{user_dir}/agents``)."""
        return self.user_dir(user_id) / "agents"

    def user_agent_dir(self, user_id: str, agent_name: str) -> Path:
        """Single agent directory (``{agents}/{name_lower}``)."""
        return self.user_agents_dir(user_id) / agent_name.lower()

    def user_agent_memory_file(self, user_id: str, agent_name: str) -> Path:
        """Per-user-per-agent memory (``{agent_dir}/memory.json``)."""
        return self.user_agent_dir(user_id, agent_name) / "memory.json"

    # -- threads -------------------------------------------------------------

    def thread_dir(self, thread_id: str, user_id: str) -> Path:
        """Per-thread directory (``{user_dir}/threads/{tid}``)."""
        return self.user_dir(user_id) / "threads" / _validate_thread_id(thread_id)

    def sandbox_user_data_dir(self, thread_id: str, user_id: str) -> Path:
        """Sandbox user-data mount root (``{thread_dir}/user-data``)."""
        return self.thread_dir(thread_id, user_id) / "user-data"

    def sandbox_work_dir(self, thread_id: str, user_id: str) -> Path:
        """Sandbox workspace → ``/mnt/user-data/workspace``."""
        return self.sandbox_user_data_dir(thread_id, user_id) / "workspace"

    def sandbox_uploads_dir(self, thread_id: str, user_id: str) -> Path:
        """Sandbox uploads → ``/mnt/user-data/uploads``."""
        return self.sandbox_user_data_dir(thread_id, user_id) / "uploads"

    def sandbox_outputs_dir(self, thread_id: str, user_id: str) -> Path:
        """Sandbox outputs → ``/mnt/user-data/outputs``."""
        return self.sandbox_user_data_dir(thread_id, user_id) / "outputs"

    def acp_workspace_dir(self, thread_id: str, user_id: str) -> Path:
        """ACP workspace → ``/mnt/acp-workspace``."""
        return self.thread_dir(thread_id, user_id) / "acp-workspace"

    def run_events_jsonl_dir(self, thread_id: str, user_id: str) -> Path:
        """JSONL run-events directory (``{thread_dir}/runs``)."""
        return self.thread_dir(thread_id, user_id) / "runs"

    # -- convenience ---------------------------------------------------------

    def ensure_data_dir(self) -> None:
        """Create the data directory (idempotent)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def ensure_user_dirs(self, user_id: str) -> None:
        """Create user-level directories (idempotent)."""
        self.user_dir(user_id).mkdir(parents=True, exist_ok=True)
        self.user_agents_dir(user_id).mkdir(parents=True, exist_ok=True)

    def ensure_thread_dirs(self, thread_id: str, user_id: str) -> None:
        """Create all standard sandbox directories for a thread.

        Directories are created with mode ``0o777`` so that Docker
        containers running as a different UID can write to them.
        """
        dirs = [
            self.sandbox_work_dir(thread_id, user_id),
            self.sandbox_uploads_dir(thread_id, user_id),
            self.sandbox_outputs_dir(thread_id, user_id),
            self.acp_workspace_dir(thread_id, user_id),
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o777)


# ---------------------------------------------------------------------------
# module-level singleton
# ---------------------------------------------------------------------------

_paths: Paths | None = None


def get_paths() -> Paths:
    """Return the global ``Paths`` singleton, creating it on first call."""
    global _paths
    if _paths is None:
        _paths = Paths()
        logger.info("Paths singleton created: base_dir=%s", _paths.base_dir)
    return _paths


def set_paths(paths: Paths) -> None:
    """Replace the global ``Paths`` singleton (e.g. for testing)."""
    global _paths
    _paths = paths
    logger.info("Paths singleton set: base_dir=%s", paths.base_dir)
