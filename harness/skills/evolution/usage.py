"""Skill usage telemetry — sidecar JSON with file locking.

Tracks per-skill usage metadata in a sidecar file:
    ~/.multiagent-studio/users/{user_id}/skills/.usage.json

Design decisions (from Hermes skill_usage.py):
  - Sidecar, not frontmatter — keeps operational telemetry out of user-authored
    SKILL.md content.
  - Atomic writes via tempfile + os.replace.
  - fcntl.flock for cross-process serialization.
  - All counter bumps are best-effort: failures log at DEBUG and return
    silently.  A broken sidecar never breaks the underlying tool call.
  - Provenance filter: only skills created_by="agent" are curator-managed.

Lifecycle states:
    active   — default, in use
    stale    — unused > stale_after_days (30)
    archived — unused > archive_after_days (90), moved to .archive/
    pinned   — opt-out from all auto transitions
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform-specific file locking
# ---------------------------------------------------------------------------

fcntl = None
msvcrt = None
try:
    import fcntl as _fcntl  # type: ignore[no-redef]
    fcntl = _fcntl
except ImportError:
    try:
        import msvcrt as _msvcrt  # type: ignore[no-redef]
        msvcrt = _msvcrt
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _user_skills_dir(user_id: str) -> Path:
    """Return ``~/.multiagent-studio/users/{user_id}/skills/``."""
    from harness.config.paths import get_paths

    return get_paths().user_skills_dir(user_id)


def _usage_file(user_id: str) -> Path:
    return _user_skills_dir(user_id) / ".usage.json"


def _archive_dir(user_id: str) -> Path:
    return _user_skills_dir(user_id) / ".archive"


# ---------------------------------------------------------------------------
# File locking context manager
# ---------------------------------------------------------------------------


@contextmanager
def _usage_file_lock(user_id: str):
    """Serialize .usage.json read-modify-write cycles across processes."""
    lock_path = _usage_file(user_id).with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[union-attr]
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            except (OSError, IOError):
                pass
        fd.close()


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO timestamp defensively for activity comparisons."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Record factory
# ---------------------------------------------------------------------------


def _empty_record() -> dict[str, Any]:
    return {
        "use_count": 0,
        "last_used_at": None,
        "patch_count": 0,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "created_by": None,  # "agent" | None (user)
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
        "absorbed_into": None,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_usage(user_id: str) -> dict[str, dict[str, Any]]:
    """Read the entire .usage.json map. Returns empty dict on missing/corrupt."""
    path = _usage_file(user_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Defensive: coerce non-dict values
    clean: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            clean[str(k)] = v
    return clean


def save_usage(user_id: str, data: dict[str, dict[str, Any]]) -> None:
    """Write the usage map atomically. Best-effort — errors are logged, not raised."""
    path = _usage_file(user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".usage_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.debug("Failed to write %s: %s", path, exc, exc_info=True)


def get_record(user_id: str, skill_name: str) -> dict[str, Any]:
    """Return the record for *skill_name*, creating a fresh one if missing."""
    data = load_usage(user_id)
    rec = data.get(skill_name)
    if not isinstance(rec, dict):
        return _empty_record()
    # Backfill missing keys from newer schema versions
    base = _empty_record()
    for k, v in base.items():
        rec.setdefault(k, v)
    return rec


# ---------------------------------------------------------------------------
# Internal mutator
# ---------------------------------------------------------------------------


def _mutate(user_id: str, skill_name: str, mutator) -> None:
    """Load, apply *mutator(record)* in place, save. Best-effort.

    Failures are logged at DEBUG and returned silently — a broken sidecar
    never breaks the underlying tool call.
    """
    if not skill_name or not user_id:
        return
    try:
        with _usage_file_lock(user_id):
            data = load_usage(user_id)
            rec = data.get(skill_name)
            if not isinstance(rec, dict):
                rec = _empty_record()
            mutator(rec)
            data[skill_name] = rec
            save_usage(user_id, data)
    except Exception as exc:
        logger.debug(
            "skill_usage._mutate(%s, %s) failed: %s",
            user_id, skill_name, exc, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Public counter-bump helpers
# ---------------------------------------------------------------------------


def bump_use(user_id: str, skill_name: str) -> None:
    """Bump use_count and last_used_at.

    Called when a skill's full SKILL.md content is loaded into a review
    fork task (indicating the skill was "in play" during the conversation).
    """

    def _apply(rec: dict[str, Any]) -> None:
        rec["use_count"] = int(rec.get("use_count") or 0) + 1
        rec["last_used_at"] = _now_iso()

    _mutate(user_id, skill_name, _apply)


def bump_patch(user_id: str, skill_name: str) -> None:
    """Bump patch_count and last_patched_at.

    Called from skill_manage on patch / edit actions.
    """

    def _apply(rec: dict[str, Any]) -> None:
        rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
        rec["last_patched_at"] = _now_iso()
        rec["last_used_at"] = _now_iso()

    _mutate(user_id, skill_name, _apply)


def mark_agent_created(user_id: str, skill_name: str) -> None:
    """Opt a skill created by skill_manage into curator management.

    Only skills with created_by="agent" are eligible for automatic
    lifecycle transitions and curator consolidation.  User-created skills
    (created_by=None) are never touched.
    """

    def _apply(rec: dict[str, Any]) -> None:
        rec["created_by"] = "agent"
        rec["last_used_at"] = _now_iso()

    _mutate(user_id, skill_name, _apply)


def forget(user_id: str, skill_name: str) -> None:
    """Drop a skill's usage entry entirely. Called when the skill is deleted."""
    if not skill_name or not user_id:
        return
    try:
        with _usage_file_lock(user_id):
            data = load_usage(user_id)
            if skill_name in data:
                del data[skill_name]
                save_usage(user_id, data)
    except Exception as exc:
        logger.debug(
            "skill_usage.forget(%s, %s) failed: %s",
            user_id, skill_name, exc, exc_info=True,
        )


# ---------------------------------------------------------------------------
# State & pin management
# ---------------------------------------------------------------------------


def set_state(user_id: str, skill_name: str, state: str) -> None:
    """Set lifecycle state. No-op if *state* is invalid."""
    if state not in _VALID_STATES:
        logger.debug("set_state: invalid state %r for %s", state, skill_name)
        return

    def _apply(rec: dict[str, Any]) -> None:
        rec["state"] = state
        if state == STATE_ARCHIVED:
            rec["archived_at"] = _now_iso()
        elif state == STATE_ACTIVE:
            rec["archived_at"] = None

    _mutate(user_id, skill_name, _apply)


def set_pinned(user_id: str, skill_name: str, pinned: bool) -> None:
    def _apply(rec: dict[str, Any]) -> None:
        rec["pinned"] = bool(pinned)

    _mutate(user_id, skill_name, _apply)


def set_absorbed_into(user_id: str, skill_name: str, umbrella: str) -> None:
    """Record that this skill was merged into *umbrella* by curator."""

    def _apply(rec: dict[str, Any]) -> None:
        rec["absorbed_into"] = umbrella

    _mutate(user_id, skill_name, _apply)


# ---------------------------------------------------------------------------
# Activity helpers
# ---------------------------------------------------------------------------


def latest_activity_at(record: dict[str, Any]) -> str | None:
    """Return the newest activity timestamp from a usage record.

    "Activity" means a skill was used or patched.  Creation time is
    excluded so callers can distinguish never-active skills.
    """
    latest_dt: datetime | None = None
    latest_raw: str | None = None
    for key in ("last_used_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso(raw)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


def activity_count(record: dict[str, Any]) -> int:
    """Return total observed activity count."""
    total = 0
    for key in ("use_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# Agent-created skill enumeration
# ---------------------------------------------------------------------------


def list_agent_created(user_id: str) -> list[str]:
    """List skill names that are curator-managed (created_by="agent").

    Scans the filesystem for SKILL.md files under the user's skills
    directory, cross-references with .usage.json.
    """
    skills_dir = _user_skills_dir(user_id)
    if not skills_dir.exists():
        return []

    usage = load_usage(user_id)
    names: list[str] = []

    for skill_md in skills_dir.rglob("SKILL.md"):
        try:
            rel = skill_md.relative_to(skills_dir)
        except ValueError:
            continue
        # Skip .archive, .hub, dot-files
        parts = rel.parts
        if parts and (parts[0].startswith(".") or parts[0] == "node_modules"):
            continue
        name = _read_skill_name(skill_md, fallback=skill_md.parent.name)
        rec = usage.get(name)
        if not isinstance(rec, dict):
            continue
        if rec.get("created_by") == "agent":
            names.append(name)

    return sorted(set(names))


def list_archived_names(user_id: str) -> list[str]:
    """List skill names in .archive/ directory."""
    archive_root = _archive_dir(user_id)
    if not archive_root.exists():
        return []
    return sorted({p.name for p in archive_root.iterdir() if p.is_dir()})


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """Parse the ``name:`` field from a SKILL.md YAML frontmatter."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def agent_created_report(user_id: str) -> list[dict[str, Any]]:
    """Return {name, state, pinned, last_activity_at, ...} for every
    agent-created skill.  Missing usage records are backfilled with defaults.
    """
    data = load_usage(user_id)
    rows: list[dict[str, Any]] = []
    for name in list_agent_created(user_id):
        rec = data.get(name)
        if not isinstance(rec, dict):
            rec = _empty_record()
        base = _empty_record()
        for k, v in base.items():
            rec.setdefault(k, v)
        row: dict[str, Any] = {"name": name, **rec}
        row["last_activity_at"] = latest_activity_at(row)
        row["activity_count"] = activity_count(row)
        rows.append(row)
    return rows
