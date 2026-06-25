"""Centralized path configuration for Harness sandbox data.

Layout mirrors DeerFlow's per-user, per-thread directory structure:

    {base_dir}/
    └── users/
        └── {user_id}/
            └── threads/
                └── {thread_id}/
                    ├── user-data/              # mounted as /mnt/user-data/
                    │   ├── workspace/          # /mnt/user-data/workspace/
                    │   ├── uploads/            # /mnt/user-data/uploads/
                    │   └── outputs/            # /mnt/user-data/outputs/
                    └── acp-workspace/          # mounted as /mnt/acp-workspace/
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path, PureWindowsPath

# Virtual path prefix seen by agents inside the sandbox
VIRTUAL_PATH_PREFIX = "/mnt/user-data"
ACP_WORKSPACE_VIRTUAL_PATH = "/mnt/acp-workspace"

_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _default_local_base_dir() -> Path:
    """Return the caller project's writable Harness state directory."""
    from harness.config import load_config

    cfg = load_config()
    return Path(cfg.data_root).expanduser().resolve()


def _validate_thread_id(thread_id: str) -> str:
    """Validate a thread ID before using it in filesystem paths."""
    if not _SAFE_THREAD_ID_RE.match(thread_id):
        raise ValueError(
            f"Invalid thread_id {thread_id!r}: only alphanumeric characters, hyphens, and underscores are allowed."
        )
    return thread_id


def _validate_user_id(user_id: str) -> str:
    """Validate a user ID before using it in filesystem paths."""
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(
            f"Invalid user_id {user_id!r}: only alphanumeric characters, hyphens, and underscores are allowed."
        )
    return user_id


def _join_host_path(base: str, *parts: str) -> str:
    """Join host filesystem path segments while preserving native style.

    Docker Desktop on Windows expects bind mount sources to stay in Windows
    path form. Using ``Path(base) / ...`` on a POSIX host can accidentally
    rewrite those paths with mixed separators, so this helper preserves the
    original style.
    """
    if not parts:
        return base

    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\") or "\\" in base:
        result = PureWindowsPath(base)
        for part in parts:
            result /= part
        return str(result)

    result = Path(base)
    for part in parts:
        result /= part
    return str(result)


class Paths:
    """Centralized path configuration for Harness application data."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def base_dir(self) -> Path:
        """Root directory for all application data."""
        if self._base_dir is not None:
            return self._base_dir
        return _default_local_base_dir()

    @property
    def data_dir(self) -> Path:
        """Alias for ``base_dir`` — used by checkpointer and persistence engine."""
        return self.base_dir

    @property
    def host_base_dir(self) -> Path:
        """Host-visible base dir for Docker volume mount sources.

        When running inside Docker with a mounted Docker socket (DooD), the
        Docker daemon runs on the host and resolves mount paths against the
        host filesystem. Set HARNESS_HOST_BASE_DIR to the host-side path that
        corresponds to this container's base_dir.
        """
        import os

        if env := os.getenv("HARNESS_HOST_BASE_DIR"):
            return Path(env)
        return self.base_dir

    def _host_base_dir_str(self) -> str:
        """Return the host base dir as a raw string for bind mounts."""
        import os

        if env := os.getenv("HARNESS_HOST_BASE_DIR"):
            return env
        return str(self.base_dir)

    def user_dir(self, user_id: str) -> Path:
        """Directory for a specific user."""
        return self.base_dir / "users" / _validate_user_id(user_id)

    def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for a thread's data: ``{base_dir}/users/{user_id}/threads/{thread_id}/``."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.user_dir(effective_user_id) / "threads" / _validate_thread_id(thread_id)

    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for the user-data root."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.thread_dir(thread_id, user_id=effective_user_id) / "user-data"

    def sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for the agent's workspace directory."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.sandbox_user_data_dir(thread_id, user_id=effective_user_id) / "workspace"

    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for user-uploaded files."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.sandbox_user_data_dir(thread_id, user_id=effective_user_id) / "uploads"

    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for agent-generated artifacts."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.sandbox_user_data_dir(thread_id, user_id=effective_user_id) / "outputs"

    def acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Host path for the ACP workspace of a specific thread."""
        effective_user_id = user_id if user_id is not None else "default"
        return self.thread_dir(thread_id, user_id=effective_user_id) / "acp-workspace"

    def host_user_dir(self, user_id: str) -> str:
        """Host path for a user directory, preserving Windows path syntax."""
        return _join_host_path(self._host_base_dir_str(), "users", _validate_user_id(user_id))

    def host_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for a thread directory, preserving Windows path syntax."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(
            self.host_user_dir(effective_user_id), "threads", _validate_thread_id(thread_id)
        )

    def host_sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for a thread's user-data root."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(self.host_thread_dir(thread_id, user_id=effective_user_id), "user-data")

    def host_sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the workspace mount source."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=effective_user_id), "workspace")

    def host_sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the uploads mount source."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=effective_user_id), "uploads")

    def host_sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the outputs mount source."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=effective_user_id), "outputs")

    def host_acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the ACP workspace mount source."""
        effective_user_id = user_id if user_id is not None else "default"
        return _join_host_path(self.host_thread_dir(thread_id, user_id=effective_user_id), "acp-workspace")

    def ensure_data_dir(self) -> None:
        """Create the base data directory if it does not exist."""
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def ensure_thread_dirs(self, thread_id: str, *, user_id: str | None = None) -> None:
        """Create all standard sandbox directories for a thread."""
        effective_user_id = user_id if user_id is not None else "default"
        for d in [
            self.sandbox_work_dir(thread_id, user_id=effective_user_id),
            self.sandbox_uploads_dir(thread_id, user_id=effective_user_id),
            self.sandbox_outputs_dir(thread_id, user_id=effective_user_id),
            self.acp_workspace_dir(thread_id, user_id=effective_user_id),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o777)

    def delete_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> None:
        """Delete all persisted data for a thread."""
        effective_user_id = user_id if user_id is not None else "default"
        thread_dir = self.thread_dir(thread_id, user_id=effective_user_id)
        if thread_dir.exists():
            shutil.rmtree(thread_dir)

    def resolve_virtual_path(
        self, thread_id: str, virtual_path: str, *, user_id: str | None = None
    ) -> Path:
        """Resolve a sandbox virtual path to the actual host filesystem path."""
        effective_user_id = user_id if user_id is not None else "default"
        stripped = virtual_path.lstrip("/")
        prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        acp_prefix = ACP_WORKSPACE_VIRTUAL_PATH.lstrip("/")

        base: Path
        relative: str
        if stripped == prefix or stripped.startswith(prefix + "/"):
            relative = stripped[len(prefix) :].lstrip("/")
            base = self.sandbox_user_data_dir(thread_id, user_id=effective_user_id).resolve()
        elif stripped == acp_prefix or stripped.startswith(acp_prefix + "/"):
            relative = stripped[len(acp_prefix) :].lstrip("/")
            base = self.acp_workspace_dir(thread_id, user_id=effective_user_id).resolve()
        else:
            raise ValueError(f"Path must start with {VIRTUAL_PATH_PREFIX} or {ACP_WORKSPACE_VIRTUAL_PATH}")

        actual = (base / relative).resolve()
        try:
            actual.relative_to(base)
        except ValueError:
            raise ValueError("Access denied: path traversal detected")

        return actual


# ── Singleton ────────────────────────────────────────────────────────────

_paths: Paths | None = None


def get_paths() -> Paths:
    """Return the global Paths singleton (lazy-initialized)."""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def set_paths(paths: Paths) -> None:
    """Set the global Paths singleton."""
    global _paths
    _paths = paths
