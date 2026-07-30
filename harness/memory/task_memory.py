"""Task memory store — structured experience extraction from completed tasks.

Task memory captures decisions, pitfalls, discoveries, and tags from completed
agent tasks.  It is stored at the project level so that memories persist across
threads within the same project.

Storage path: ``{base_dir}/users/{uid}/projects/{pid}/memory/tasks/{task_id}.json``
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config.paths import get_paths

logger = logging.getLogger(__name__)

# ── stop words for keyword extraction ──
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again",
    "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "now", "its", "it", "and", "but", "or",
    "this", "that", "these", "those", "which", "who", "whom",
    # Chinese stop words
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "所", "被", "把", "让", "向", "从", "与", "对", "而", "但", "且", "或",
})


# ──────────────────────────────────────────────────────────────────────────────
# TaskMemory dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskMemory:
    """Structured memory extracted from a completed task."""

    task_id: str
    task_title: str
    assigned_agent: str = ""
    status: str = ""  # "completed" | "approved" | "failed"
    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    discoveries: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_title": self.task_title,
            "assigned_agent": self.assigned_agent,
            "status": self.status,
            "summary": self.summary,
            "decisions": self.decisions,
            "pitfalls": self.pitfalls,
            "discoveries": self.discoveries,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskMemory:
        return cls(
            task_id=data.get("task_id", ""),
            task_title=data.get("task_title", ""),
            assigned_agent=data.get("assigned_agent", ""),
            status=data.get("status", ""),
            summary=data.get("summary", ""),
            decisions=data.get("decisions", []),
            pitfalls=data.get("pitfalls", []),
            discoveries=data.get("discoveries", []),
            tags=data.get("tags", []),
            created_at=data.get("created_at", ""),
        )


# ──────────────────────────────────────────────────────────────────────────────
# TaskMemoryStore
# ──────────────────────────────────────────────────────────────────────────────

class TaskMemoryStore:
    """Persistent storage for task memories.

    Memories are stored as individual JSON files under::

        {base_dir}/users/{uid}/projects/{pid}/memory/tasks/{task_id}.json
    """

    def __init__(self, project_id: str, user_id: str = "default") -> None:
        paths = get_paths()
        self._memory_dir: Path = (
            paths.base_dir / "users" / user_id / "projects" / project_id
            / "memory" / "tasks"
        )
        self._memory_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    async def save(self, memory: TaskMemory) -> None:
        """Save a task memory to its JSON file (atomic write)."""
        path = self._memory_dir / f"{memory.task_id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        data = memory.to_dict()
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
        logger.debug("Task memory saved: %s (%s)", memory.task_id, memory.task_title)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    async def load(self, task_id: str) -> TaskMemory | None:
        """Load a single task memory by ID."""
        path = self._memory_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return TaskMemory.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load task memory '%s': %s", task_id, exc)
            return None

    async def find_related(
        self,
        title: str,
        description: str = "",
        max_results: int = 3,
    ) -> list[TaskMemory]:
        """Find related task memories via keyword overlap with title and tags.

        Uses simple keyword extraction (split + stop-word removal) from the
        query title/description and scores each stored memory by overlap with
        its title + tags.  Returns up to *max_results* memories sorted by
        relevance (descending).
        """
        if max_results <= 0:
            return []

        # ── extract keywords from query ──
        query_text = f"{title} {description}".lower()
        query_keywords = _extract_keywords(query_text)
        if not query_keywords:
            return []

        # ── scan all memory files ──
        files = list(self._memory_dir.glob("*.json"))
        if not files:
            return []

        scored: list[tuple[int, TaskMemory]] = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            memory = TaskMemory.from_dict(data)
            # only include "completed" / "approved" / "failed" statuses
            if memory.status not in ("completed", "approved", "failed"):
                continue

            # build target text from title + tags
            target_text = f"{memory.task_title} {' '.join(memory.tags)}".lower()
            target_keywords = _extract_keywords(target_text)
            if not target_keywords:
                continue

            # overlap score
            overlap = len(query_keywords & target_keywords)
            if overlap > 0:
                scored.append((overlap, memory))

        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:max_results]]

    async def list_all(self) -> list[TaskMemory]:
        """List all task memories, newest first."""
        files = sorted(
            self._memory_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        memories: list[TaskMemory] = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                memories.append(TaskMemory.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        return memories


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> set[str]:
    """Extract significant lowercase keywords from text."""
    # split on whitespace and common punctuation
    import re

    tokens = re.split(r"[\s,，、。.!！?？:：;；\-—/\\()（）\[\]【】《》]+", text)
    keywords: set[str] = set()
    for token in tokens:
        token = token.strip().lower()
        if len(token) < 2:
            continue
        if token in _STOP_WORDS:
            continue
        # skip pure numbers
        if token.replace(".", "").replace("-", "").isdigit():
            continue
        keywords.add(token)
    return keywords
