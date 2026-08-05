"""Memory storage providers — adapted from harness.

Single JSON file per user: ``{memory_root}/users/{user_id}/memory.json``
"""

import abc
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.config.memory_config import get_memory_config
from harness.memory.safety import sanitize_memory_if_unsafe, validate_memory_json

logger = logging.getLogger(__name__)


def utc_now_iso_z() -> str:
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """Create an empty memory structure matching the canonical schema."""
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z(),
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
            "avoidances": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentWeeks": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


class MemoryStorage(abc.ABC):
    """Abstract base class for memory storage providers."""

    @abc.abstractmethod
    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Load memory data for the given scope."""
        ...

    @abc.abstractmethod
    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Force reload memory data, bypassing cache."""
        ...

    @abc.abstractmethod
    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *,
             user_id: str | None = None) -> bool:
        """Save memory data."""
        ...


class FileMemoryStorage(MemoryStorage):
    """File-based memory storage — single JSON per user."""

    def __init__(self, memory_root: str | None = None):
        if memory_root:
            self._memory_root = Path(memory_root)
        else:
            try:
                cfg = get_memory_config()
                self._memory_root = Path(cfg.storage_path) if cfg.storage_path else Path.home() / ".multiagent-studio" / "memory"
            except Exception:
                self._memory_root = Path.home() / ".multiagent-studio" / "memory"

        self._memory_cache: dict[tuple[str | None, str | None], tuple[dict[str, Any], float | None]] = {}
        self._cache_lock = threading.Lock()

    def _get_memory_file_path(self, agent_name: str | None = None, *,
                              user_id: str | None = None) -> Path:
        """Resolve the memory file path under the configured memory root.

        Layout::

            {memory_root}/users/{user_id}/memory.json
            {memory_root}/users/{user_id}/agents/{agent_name}/memory.json
        """
        uid = user_id or "default"
        if agent_name:
            return self._memory_root / "users" / uid / "agents" / agent_name / "memory.json"
        return self._memory_root / "users" / uid / "memory.json"

    def _load_memory_from_file(self, agent_name: str | None = None, *,
                               user_id: str | None = None) -> dict[str, Any]:
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        if not file_path.exists():
            return create_empty_memory()
        try:
            with open(file_path, encoding="utf-8") as f:
                memory_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load memory file: %s", e)
            return create_empty_memory()

        # ── 安全检测: 提示注入 / 凭证外泄 / 不可见字符 ──
        safe_data, findings = sanitize_memory_if_unsafe(
            memory_data, source=str(file_path),
        )
        if findings:
            return create_empty_memory()

        # ── 惰性清理: 过滤过期 facts ──
        memory_data = self._maybe_cleanup_expired(safe_data)
        return memory_data

    def _maybe_cleanup_expired(self, memory_data: dict[str, Any]) -> dict[str, Any]:
        """惰性清理过期 facts (仅当 TTL > 0 时生效)."""
        cfg = get_memory_config()
        ttl_days = getattr(cfg, "memory_ttl_days", 0)
        if ttl_days <= 0:
            return memory_data

        facts: list[dict] = memory_data.get("facts", [])
        if not facts:
            return memory_data

        from datetime import UTC, datetime, timedelta
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        cutoff_iso = cutoff.isoformat()

        before = len(facts)
        memory_data["facts"] = [
            f for f in facts
            if f.get("createdAt", "") >= cutoff_iso
        ]
        after = len(memory_data["facts"])
        if after < before:
            logger.info(
                "Lazy cleanup: removed %d expired facts from memory file (ttl=%d days)",
                before - after, ttl_days,
            )
        return memory_data

    @staticmethod
    def _cache_key(agent_name: str | None = None, *,
                   user_id: str | None = None) -> tuple[str | None, str | None]:
        return (user_id, agent_name)

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)
        try:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            current_mtime = None

        with self._cache_lock:
            cached = self._memory_cache.get(cache_key)
            if cached is not None and cached[1] == current_mtime:
                return cached[0]

        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)
        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, current_mtime)
        return memory_data

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)
        try:
            mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            mtime = None
        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, mtime)
        return memory_data

    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *,
             user_id: str | None = None) -> bool:
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)

        # ── 安全检测: 写入前校验 ──
        findings = validate_memory_json(memory_data, source=str(file_path))
        if findings:
            logger.error(
                "Memory save BLOCKED for %s — safety findings: %s",
                file_path, ", ".join(findings),
            )
            return False

        cache_key = self._cache_key(agent_name, user_id=user_id)
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}
            temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            # ── 修复 #11: 文件锁防多进程写入覆盖 ──
            import fcntl
            lock_path = file_path.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(memory_data, f, indent=2, ensure_ascii=False)
                    temp_path.replace(file_path)
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None
            with self._cache_lock:
                self._memory_cache[cache_key] = (memory_data, mtime)
            logger.debug("Memory saved to %s", file_path)
            return True
        except OSError as e:
            logger.error("Failed to save memory file: %s", e)
            return False


# ── Global singleton ──────────────────────────────────────────────────────
_storage_instance: MemoryStorage | None = None
_storage_lock = threading.Lock()


def get_memory_storage() -> MemoryStorage:
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance
    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance
        _storage_instance = FileMemoryStorage()
    return _storage_instance
