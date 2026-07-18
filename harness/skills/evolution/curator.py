"""Curator — periodic skill consolidation orchestrator.

Runs infrequently (every 7 days + 2 hours of system idle time) and forks a
review agent to consolidate fragmented narrow skills into class-level
umbrella skills.

Design decisions (from Hermes agent/curator.py):
  - Idle-triggered, not cron — ``should_run_now()`` checks last run time and
    user activity window.
  - Only touches agent-created skills (created_by="agent" in .usage.json).
  - Never hard-deletes — only archives (recoverable).
  - Pinned skills bypass all curator operations.
  - Dry-run mode available for preview.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_HOURS = 24 * 7  # 7 days
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_MAX_REVIEW_TURNS = 24  # curator needs more turns than regular review


# ---------------------------------------------------------------------------
# .curator_state — persistent scheduler
# ---------------------------------------------------------------------------


def _state_file(user_id: str) -> Path:
    from harness.skills.evolution.usage import _user_skills_dir
    return _user_skills_dir(user_id) / ".curator_state"


def _default_state() -> dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_duration_seconds": None,
        "last_run_summary": None,
        "paused": False,
        "run_count": 0,
    }


def load_curator_state(user_id: str) -> dict[str, Any]:
    path = _state_file(user_id)
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    state.update({k: v for k, v in data.items() if k in state})
    return state


def save_curator_state(user_id: str, state: dict[str, Any]) -> None:
    path = _state_file(user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".curator_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("Failed to save curator state: %s", exc)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def should_run_now(
    user_id: str,
    last_activity: datetime | None = None,
    now: datetime | None = None,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    min_idle_hours: int = DEFAULT_MIN_IDLE_HOURS,
) -> bool:
    """Check whether curator should run for *user_id*.

    Conditions (all must be true):
    1. Not paused.
    2. Last run was > *interval_hours* ago (or never run).
    3. Last user activity was > *min_idle_hours* ago.
    """
    state = load_curator_state(user_id)
    if state.get("paused"):
        return False

    if now is None:
        now = datetime.now(timezone.utc)

    last_run_raw = state.get("last_run_at")
    if last_run_raw:
        try:
            last_run = datetime.fromisoformat(last_run_raw)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            if now - last_run < timedelta(hours=interval_hours):
                return False
        except (TypeError, ValueError):
            pass

    if last_activity is not None and min_idle_hours > 0:
        if now - last_activity < timedelta(hours=min_idle_hours):
            return False

    return True


# ---------------------------------------------------------------------------
# CURATOR_REVIEW_PROMPT — adapted from Hermes
# ---------------------------------------------------------------------------


CURATOR_REVIEW_PROMPT = """\
You are running as the background skill CURATOR. This is an UMBRELLA-BUILDING
consolidation pass, not a passive audit and not a duplicate-finder.

The goal of the skill collection is a LIBRARY OF CLASS-LEVEL SKILLS. A
collection of dozens of narrow skills where each one captures one session's
specific bug is a FAILURE of the library. One broad umbrella skill with
labeled subsections beats five narrow siblings for discoverability.

Hard rules — do not violate:
1. DO NOT touch bundled or built-in skills. The candidate list below is
   already filtered to agent-created skills only.
2. DO NOT delete any skill. Archiving (moving to .archive/) is the maximum
   destructive action. Archives are recoverable; deletion is not.
3. DO NOT touch skills shown as pinned=yes. Skip them entirely.
4. DO NOT use usage counters as a reason to skip consolidation. The counters
   are often mostly zero. Judge overlap on CONTENT, not on use_count.
5. DO NOT reject consolidation because "each skill has a distinct trigger".
   The right bar is: "would a human maintainer write these as N separate
   skills, or as one skill with N labeled subsections?"

How to work:
1. Scan the full candidate list. Identify PREFIX CLUSTERS — skills sharing
   a first word or domain keyword. Expect 10-25 clusters.
2. For each cluster with 2+ members, ask "what is the UMBRELLA CLASS these
   skills all serve?" Merge accordingly.
3. Three ways to consolidate — use the right one per cluster:
   a. MERGE INTO EXISTING UMBRELLA — one skill is already broad enough.
      Patch it to add labeled sections for each sibling's unique insight,
      then archive the siblings (action=delete with absorbed_into=<umbrella>).
   b. CREATE A NEW UMBRELLA — no existing member is broad enough. Use
      skill_manage action=create to write a new class-level skill whose
      SKILL.md covers the shared workflow with labeled subsections.
      Archive the now-absorbed siblings.
   c. DEMOTE TO SUPPORT FILE — a sibling has narrow-but-valuable session-
      specific content. Write it into the umbrella's references/ directory
      via skill_manage action=write_file. Archive the old sibling.
4. Flag skills whose NAME is too narrow (contains a PR number, error string,
   codename). These belong as a subsection under an umbrella.
5. Iterate. After one consolidation round, scan for the next umbrella
   opportunity. Expect at least 10 archives.

Your only tool is skill_manage (create / patch / write_file / delete).
- action=delete with absorbed_into=<umbrella> archives a skill and records
  the forwarding target.
- action=write_file with relative_path starting "references/" adds a support
  file under an existing umbrella's directory.
- The umbrella's SKILL.md should gain a one-line pointer to any new support
  file so future agents find it.

"keep" is legitimate ONLY when the skill is already a class-level umbrella.
"This is narrow but distinct" is NOT a reason to keep — it's a reason to
move it under an umbrella as a subsection or support file.

When done, produce a summary of every action taken (or proposed in dry-run).
List each cluster, what was merged, and where the content ended up.
If fewer than 5 archives, go back and scan the clusters again."""


DRY_RUN_BANNER = """\
DRY-RUN — REPORT ONLY. DO NOT MUTATE THE SKILL LIBRARY.

This is a PREVIEW pass. Follow every instruction EXCEPT:
  - DO NOT call skill_manage with action=create, patch, delete, or write_file.
  - DO NOT move or delete any files.

Your output IS the deliverable. Describe the actions you WOULD take.
After review, the operator will run `curator run` (no dry-run flag)."""


# ---------------------------------------------------------------------------
# Candidate list builder
# ---------------------------------------------------------------------------


def _build_candidate_list(user_id: str) -> str:
    """Build a human-readable candidate list for the curator prompt."""
    from harness.skills.evolution.usage import agent_created_report

    rows = agent_created_report(user_id)
    if not rows:
        return "(no agent-created skills)"

    lines: list[str] = []
    for r in rows:
        pinned = " [PINNED]" if r.get("pinned") else ""
        state = r.get("state", "active")
        activity = r.get("last_activity_at") or "never"
        lines.append(
            f"  - {r['name']} | state={state} | last_activity={activity[:10]}"
            f" | use={r.get('use_count', 0)} | patch={r.get('patch_count', 0)}{pinned}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def maybe_run_curator(
    user_id: str,
    skill_storage: Any,
    llm_factory: Any,
    model: str,
    *,
    dry_run: bool = False,
    last_activity: datetime | None = None,
) -> list[str]:
    """Check gate, then fork a curator review agent to consolidate skills.

    Called from ``HarnessService.initialize()`` or a periodic gateway tick.
    Never raises — failures are logged and discarded.

    Returns:
        Human-readable action summary list (empty if nothing was done).
    """
    now = datetime.now(timezone.utc)

    if not should_run_now(user_id, last_activity=last_activity, now=now):
        logger.debug("Curator: should_run_now=False for user=%s", user_id)
        return []

    # Run lifecycle transitions before curator review
    try:
        from harness.skills.evolution.lifecycle import apply_automatic_transitions
        transitions = apply_automatic_transitions(user_id, now=now)
        logger.info(
            "Curator: lifecycle transitions for user=%s: %s", user_id, transitions,
        )
    except Exception:
        logger.warning("Curator: lifecycle transitions failed", exc_info=True)

    # Build candidate list
    candidate_list = _build_candidate_list(user_id)
    if "(no agent-created skills)" in candidate_list:
        logger.info("Curator: no agent-created skills for user=%s — skipping", user_id)
        return []

    # Build task
    banner = DRY_RUN_BANNER if dry_run else ""
    task = f"{banner}\n\n{CURATOR_REVIEW_PROMPT}\n\n---\n\n## Candidate Skills\n\n{candidate_list}"

    logger.info(
        "Curator: starting for user=%s (dry_run=%s, candidates=%d)",
        user_id, dry_run, candidate_list.count("\n  - "),
    )

    try:
        from harness.models import SubAgentConfig
        from harness.skills.evolution.provenance import ORIGIN_CURATOR, set_write_origin
        from harness.skills.evolution.review_fork import ReviewAgent, _extract_actions
        from harness.tools.skill_manage_tool import (
            allow_skill_manage,
            create_skill_manage_tool,
            set_skill_user_id,
        )

        set_write_origin(ORIGIN_CURATOR)
        set_skill_user_id(user_id)
        allow_skill_manage()

        llm = llm_factory(model)
        skill_manage = create_skill_manage_tool(
            skill_storage=skill_storage,
            model_client=llm,
        )

        config = SubAgentConfig(
            name="_curator",
            display_name="Curator",
            description="Background curator that consolidates skills into umbrella skills.",
            system_prompt=(
                "You are a skill curator. Consolidate fragmented narrow skills "
                "into class-level umbrella skills. Use skill_manage to create, "
                "patch, write support files, and archive merged skills."
            ),
            model="inherit",
            tools=None,
            disallowed_tools=[],
            max_turns=DEFAULT_MAX_REVIEW_TURNS,
            timeout_seconds=900,
        )

        review_agent = ReviewAgent(
            config=config,
            llm=llm,
            tools=[skill_manage],
            user_id=user_id,
        )

        result = await review_agent.run(task)
        actions = _extract_actions(result)

        # Update curator state
        state = load_curator_state(user_id)
        state["last_run_at"] = now.isoformat()
        state["run_count"] = state.get("run_count", 0) + 1
        if actions:
            state["last_run_summary"] = " · ".join(actions)
        else:
            state["last_run_summary"] = "no actions taken"
        save_curator_state(user_id, state)

        # Notify user on next turn
        if actions:
            from harness.skills.evolution.review_fork import _notifications
            summary = "Curator: " + " · ".join(actions)
            _notifications.setdefault(user_id, []).append(summary)
            logger.info("Curator completed for user=%s: %s", user_id, summary)
        else:
            logger.info("Curator completed for user=%s: no actions", user_id)

        return actions

    except Exception:
        logger.warning("Curator run failed for user=%s", user_id, exc_info=True)
        return []
