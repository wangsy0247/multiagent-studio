"""Skill lifecycle state machine — automatic active → stale → archived.

Pure-function transitions with no LLM calls.  Compares activity timestamps
from ``.usage.json`` against configurable day thresholds.

Design decisions (from Hermes curator.py:apply_automatic_transitions):
  - Pinned skills are NEVER touched.
  - Archive means moving the skill directory into ``.archive/`` — never
    hard-deletion.  Archives are always recoverable via ``restore_skill``.
  - Reactivation: a stale skill that gets used again is automatically
    moved back to ``active``.
  - Only agent-created skills (created_by="agent") are eligible.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.skills.evolution.usage import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    _archive_dir,
    _parse_iso,
    _user_skills_dir,
    agent_created_report,
    set_state,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable thresholds (overridable via SkillEvolutionConfig)
# ---------------------------------------------------------------------------

DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90

_stale_after_days = DEFAULT_STALE_AFTER_DAYS
_archive_after_days = DEFAULT_ARCHIVE_AFTER_DAYS


def get_thresholds() -> tuple[int, int]:
    """Return (stale_after_days, archive_after_days)."""
    return _stale_after_days, _archive_after_days


def set_thresholds(stale_days: int, archive_days: int) -> None:
    """Override lifecycle thresholds."""
    global _stale_after_days, _archive_after_days
    _stale_after_days = stale_days
    _archive_after_days = archive_days


# ---------------------------------------------------------------------------
# Automatic transitions (pure function)
# ---------------------------------------------------------------------------


def apply_automatic_transitions(
    user_id: str, now: datetime | None = None,
) -> dict[str, int]:
    """Walk every agent-created skill and apply lifecycle transitions.

    Args:
        user_id: Current user identifier.
        now: Timestamp to use as "now" (injectable for tests).

    Returns:
        Counter dict: ``{marked_stale, archived, reactivated, checked}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stale_cutoff = now - timedelta(days=_stale_after_days)
    archive_cutoff = now - timedelta(days=_archive_after_days)

    counts: dict[str, int] = {
        "marked_stale": 0, "archived": 0, "reactivated": 0, "checked": 0,
    }

    for row in agent_created_report(user_id):
        counts["checked"] += 1
        name: str = row["name"]
        if row.get("pinned"):
            continue

        last_activity = _parse_iso(row.get("last_activity_at"))
        # If never active, use created_at as anchor so new skills don't
        # immediately archive themselves.
        anchor = last_activity or _parse_iso(row.get("created_at")) or now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        current = row.get("state", STATE_ACTIVE)

        if anchor <= archive_cutoff and current != STATE_ARCHIVED:
            ok, _msg = archive_skill(user_id, name)
            if ok:
                counts["archived"] += 1
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            set_state(user_id, name, STATE_STALE)
            counts["marked_stale"] += 1
        elif anchor > stale_cutoff and current == STATE_STALE:
            # Skill got used again after being marked stale — reactivate.
            set_state(user_id, name, STATE_ACTIVE)
            counts["reactivated"] += 1

    if any(v > 0 for v in counts.values()):
        logger.info(
            "Lifecycle transitions for user=%s: %s", user_id, counts,
        )

    return counts


# ---------------------------------------------------------------------------
# Archive / restore
# ---------------------------------------------------------------------------


def archive_skill(user_id: str, skill_name: str) -> tuple[bool, str]:
    """Move an agent-created skill directory to .archive/.

    Returns (ok, message).  Never hard-deletes.
    """
    skill_dir = _find_skill_dir(user_id, skill_name)
    if skill_dir is None:
        return False, f"skill '{skill_name}' not found"

    archive_root = _archive_dir(user_id)
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"failed to create archive dir: {exc}"

    dest = archive_root / skill_dir.name
    if dest.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = archive_root / f"{skill_dir.name}-{ts}"

    try:
        skill_dir.rename(dest)
    except OSError:
        try:
            shutil.move(str(skill_dir), str(dest))
        except Exception as exc:
            return False, f"failed to archive: {exc}"

    set_state(user_id, skill_name, STATE_ARCHIVED)
    logger.info("Archived skill '%s' → %s", skill_name, dest)
    return True, f"archived to {dest}"


def restore_skill(user_id: str, skill_name: str) -> tuple[bool, str]:
    """Move an archived skill back to the user skills directory.

    Returns (ok, message).
    """
    archive_root = _archive_dir(user_id)
    if not archive_root.exists():
        return False, "no archive directory"

    # Try exact name first, then timestamped variants
    candidates = sorted(
        [p for p in archive_root.rglob("*")
         if p.is_dir() and (p.name == skill_name or p.name.startswith(f"{skill_name}-"))],
        reverse=True,
    )
    if not candidates:
        return False, f"skill '{skill_name}' not found in archive"

    src = candidates[0]
    dest = _user_skills_dir(user_id) / skill_name
    if dest.exists():
        return False, f"destination already exists: {dest}"

    try:
        src.rename(dest)
    except OSError:
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            return False, f"failed to restore: {exc}"

    set_state(user_id, skill_name, STATE_ACTIVE)
    logger.info("Restored skill '%s' from archive", skill_name)
    return True, f"restored to {dest}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_skill_dir(user_id: str, skill_name: str) -> Path | None:
    """Locate the directory for a skill by its frontmatter ``name:`` field.

    Handles both flat and category-nested layouts.
    """
    base = _user_skills_dir(user_id)
    if not base.exists():
        return None
    for skill_md in base.rglob("SKILL.md"):
        try:
            rel = skill_md.relative_to(base)
        except ValueError:
            continue
        if rel.parts and rel.parts[0].startswith("."):
            continue
        from harness.skills.evolution.usage import _read_skill_name

        if _read_skill_name(skill_md, fallback=skill_md.parent.name) == skill_name:
            return skill_md.parent
    return None
