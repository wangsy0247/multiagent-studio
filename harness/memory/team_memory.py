"""Team memory store — shared team-level knowledge accumulated across runs.

Team memory captures best practices, known pitfalls, and recent run summaries
at the project level so the team gets smarter with every run.

Storage path: ``{base_dir}/users/{uid}/projects/{pid}/memory/team_memory.json``
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)

# ── limits ──
MAX_BEST_PRACTICES = 20
MAX_PITFALLS = 20
MAX_RECENT_RUNS = 5


# ──────────────────────────────────────────────────────────────────────────────
# TeamMemory dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TeamMemory:
    """Accumulated team knowledge shared across runs within a project."""

    best_practices: list[dict] = field(default_factory=list)
    known_pitfalls: list[dict] = field(default_factory=list)
    recent_runs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "best_practices": self.best_practices,
            "known_pitfalls": self.known_pitfalls,
            "recent_runs": self.recent_runs,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeamMemory:
        return cls(
            best_practices=data.get("best_practices", []),
            known_pitfalls=data.get("known_pitfalls", []),
            recent_runs=data.get("recent_runs", []),
        )


# ──────────────────────────────────────────────────────────────────────────────
# TeamMemoryStore
# ──────────────────────────────────────────────────────────────────────────────

class TeamMemoryStore:
    """Persistent storage for team-level memory.

    Stores a single ``team_memory.json`` per project::

        {base_dir}/users/{uid}/projects/{pid}/memory/team_memory.json
    """

    def __init__(self, project_id: str, user_id: str = "default") -> None:
        paths = get_paths()
        self._path: Path = (
            paths.base_dir / "users" / user_id / "projects" / project_id
            / "memory" / "team_memory.json"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # read / write
    # ------------------------------------------------------------------

    async def load(self) -> TeamMemory:
        """Load team memory, returning empty structure if file doesn't exist."""
        if not self._path.exists():
            return TeamMemory()
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return TeamMemory.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load team memory: %s", exc)
            return TeamMemory()

    async def save(self, memory: TeamMemory) -> None:
        """Atomically write team memory to disk."""
        tmp_path = self._path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(memory.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._path)
        logger.debug("Team memory saved: %d practices, %d pitfalls, %d runs",
                     len(memory.best_practices), len(memory.known_pitfalls),
                     len(memory.recent_runs))

    # ------------------------------------------------------------------
    # merge
    # ------------------------------------------------------------------

    async def merge_updates(
        self,
        new_practices: list[dict] | None = None,
        new_pitfalls: list[dict] | None = None,
        run_summary: dict | None = None,
    ) -> TeamMemory:
        """Incrementally merge new entries into existing team memory.

        Deduplicates by ``practice`` / ``pitfall`` text (case-insensitive).
        Enforces entry caps: practices ≤ 20, pitfalls ≤ 20, runs ≤ 5.
        """
        current = await self.load()

        # ── merge best practices (dedup by practice text) ──
        if new_practices:
            existing_texts = {
                p.get("practice", "").strip().lower() for p in current.best_practices
            }
            for p in new_practices:
                key = p.get("practice", "").strip().lower()
                if key and key not in existing_texts:
                    p["at"] = p.get("at") or datetime.now(timezone.utc).isoformat()
                    current.best_practices.append(p)
                    existing_texts.add(key)
            # sort: critical > high > medium, then by recency
            priority_order = {"critical": 0, "high": 1, "medium": 2}
            current.best_practices.sort(
                key=lambda x: (priority_order.get(x.get("importance", "medium"), 2),
                               x.get("at", "")))
            current.best_practices = current.best_practices[:MAX_BEST_PRACTICES]

        # ── merge pitfalls (dedup by pitfall text) ──
        if new_pitfalls:
            existing_texts = {
                p.get("pitfall", "").strip().lower() for p in current.known_pitfalls
            }
            for p in new_pitfalls:
                key = p.get("pitfall", "").strip().lower()
                if key and key not in existing_texts:
                    p["at"] = p.get("at") or datetime.now(timezone.utc).isoformat()
                    current.known_pitfalls.append(p)
                    existing_texts.add(key)
            current.known_pitfalls.sort(key=lambda x: x.get("at", ""), reverse=True)
            current.known_pitfalls = current.known_pitfalls[:MAX_PITFALLS]

        # ── merge recent runs (dedup by thread_id) ──
        if run_summary:
            existing_ids = {r.get("thread_id") for r in current.recent_runs}
            tid = run_summary.get("thread_id", "")
            if tid and tid not in existing_ids:
                run_summary["at"] = run_summary.get("at") or datetime.now(timezone.utc).isoformat()
                current.recent_runs.append(run_summary)
            current.recent_runs.sort(key=lambda x: x.get("at", ""), reverse=True)
            current.recent_runs = current.recent_runs[:MAX_RECENT_RUNS]

        await self.save(current)
        return current

    # ------------------------------------------------------------------
    # context XML (for injection into system prompt)
    # ------------------------------------------------------------------

    async def get_context_xml(self) -> str:
        """Generate a compact ``<team_memory>`` XML block for prompt injection.

        Targets ~300 tokens max: only critical/high practices, top 3 pitfalls,
        and the most recent run.
        """
        mem = await self.load()
        if (not mem.best_practices and not mem.known_pitfalls
                and not mem.recent_runs):
            return ""

        lines = ["<team_memory>"]

        # best practices: critical + high only (most impactful)
        important = [p for p in mem.best_practices
                     if p.get("importance") in ("critical", "high")]
        if important:
            lines.append("Team best practices:")
            for p in important[:5]:
                lines.append(f"- [{p.get('importance', 'medium')}] {p.get('practice', '')}")

        # known pitfalls: top 3 most recent
        if mem.known_pitfalls:
            lines.append("\nKnown pitfalls:")
            for p in mem.known_pitfalls[:3]:
                affected = p.get("affected", [])
                if isinstance(affected, list) and affected:
                    affected_str = ", ".join(affected)
                    lines.append(f"- {p.get('pitfall', '')} (affects: {affected_str})")
                else:
                    lines.append(f"- {p.get('pitfall', '')}")

        # recent runs: last 1-2
        if mem.recent_runs:
            lines.append("\nRecent runs:")
            for r in mem.recent_runs[:2]:
                lines.append(
                    f"- [{r.get('thread_id', '?')}] {r.get('summary', '')} "
                    f"({r.get('tasks_completed', 0)} completed, {r.get('tasks_failed', 0)} failed)"
                )

        lines.append("</team_memory>")
        return "\n".join(lines)
