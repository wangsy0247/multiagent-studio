"""Local-filesystem skill storage — discovery, loading, and CRUD.

Adapted from DeerFlow's ``local_skill_storage.py`` and ``skill_storage.py``
base class.  Uses a single project-root directory layout:

    <root>/
    ├── public/<name>/SKILL.md    ← built-in skills (git-tracked, read-only)
    ├── custom/<name>/SKILL.md    ← user skills (.gitignored, editable)
    └── custom/.history/<name>.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .parser import parse_skill_file
from .types import SKILL_MD_FILE, Skill, SkillCategory

logger = logging.getLogger(__name__)

# Skill name convention: lowercase letters, digits, hyphens.
_SKILL_NAME_PATTERN = __import__("re").compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillStorage:
    """Local-filesystem skill storage (single project-root directory)."""

    def __init__(self, root_path: Path, container_path: str = "/mnt/skills") -> None:
        self._root = root_path
        self._container_root = container_path

    # ------------------------------------------------------------------
    # static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def validate_skill_name(name: str) -> str:
        """Validate and normalise a skill name; return the normalised form."""
        normalized = name.strip()
        if not _SKILL_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Skill name must be hyphen-case using lowercase letters, "
                "digits, and hyphens only."
            )
        if len(normalized) > 64:
            raise ValueError("Skill name must be 64 characters or fewer.")
        return normalized

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def get_skills_root_path(self) -> Path:
        """Host path to the skills root (used for sandbox mounts)."""
        return self._root

    def get_container_root(self) -> str:
        """Container path where skills are mounted (e.g. ``/mnt/skills``)."""
        return self._container_root

    def get_custom_skill_dir(self, name: str) -> Path:
        """Path to ``custom/<name>``. Does not create the directory."""
        normalized = self.validate_skill_name(name)
        return self._root / SkillCategory.CUSTOM.value / normalized

    def get_custom_skill_file(self, name: str) -> Path:
        """Path to ``custom/<name>/SKILL.md``."""
        return self.get_custom_skill_dir(name) / SKILL_MD_FILE

    def get_skill_history_file(self, name: str) -> Path:
        """Path to ``custom/.history/<name>.jsonl``."""
        normalized = self.validate_skill_name(name)
        return (
            self._root
            / SkillCategory.CUSTOM.value
            / ".history"
            / f"{normalized}.jsonl"
        )

    def custom_skill_exists(self, name: str) -> bool:
        return self.get_custom_skill_file(name).exists()

    def public_skill_exists(self, name: str) -> bool:
        return (
            self._root / SkillCategory.PUBLIC.value / name / SKILL_MD_FILE
        ).exists()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
        """Yield ``(category, category_root, skill_md_path)`` for every SKILL.md."""
        if not self._root.exists():
            return
        for category in SkillCategory:
            category_path = self._root / category.value
            if not category_path.exists() or not category_path.is_dir():
                continue
            for current_root, dir_names, file_names in os.walk(
                category_path, followlinks=True
            ):
                # Skip hidden directories.
                dir_names[:] = sorted(
                    name for name in dir_names if not name.startswith(".")
                )
                if SKILL_MD_FILE not in file_names:
                    continue
                yield category, category_path, Path(current_root) / SKILL_MD_FILE

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def load_skills(self, *, enabled_only: bool = False) -> list[Skill]:
        """Discover all skills, merge enabled state, sort, and optionally filter.

        Re-reads ``extensions_config.json`` on every call so external changes
        (e.g. made by another process or the frontend) are picked up immediately.
        """
        skills_by_name: dict[str, Skill] = {}

        # 1. Discover and parse every SKILL.md.
        for category, category_root, md_path in self._iter_skill_files():
            skill = parse_skill_file(
                md_path,
                category=category,
                relative_path=md_path.parent.relative_to(category_root),
            )
            if skill is None:
                continue
            # custom overrides public when names collide.
            skills_by_name[skill.name] = skill

        skills = list(skills_by_name.values())

        # 2. Merge enabled state from extensions_config.json.
        try:
            from harness.config.extensions_config import ExtensionsConfig

            extensions_config = ExtensionsConfig.from_file()
            for skill in skills:
                skill.enabled = extensions_config.is_skill_enabled(
                    skill.name, skill.category
                )
        except Exception as exc:
            logger.warning("Failed to load extensions config: %s", exc)

        # 3. Filter and sort.
        if enabled_only:
            skills = [s for s in skills if s.enabled]
        skills.sort(key=lambda s: s.name)
        return skills

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def read_custom_skill(self, name: str) -> str:
        """Read the SKILL.md content for a custom skill."""
        if not self.custom_skill_exists(name):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        return (self.get_custom_skill_dir(name) / SKILL_MD_FILE).read_text(
            encoding="utf-8"
        )

    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        """Atomically write a text file under ``custom/<name>/<relative_path>``.

        Uses tempfile + rename to guarantee atomicity.
        """
        target_dir = self.get_custom_skill_dir(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = (target_dir / relative_path).resolve()

        # Security: ensure target stays within the skill directory.
        resolved_base = target_dir.resolve()
        try:
            target.relative_to(resolved_base)
        except ValueError:
            raise ValueError(
                "relative_path must resolve within the skill directory."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(target.parent),
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = Path(tmp_file.name)
        tmp_path.replace(target)

    def delete_custom_skill(self, name: str) -> None:
        """Delete a custom skill directory entirely."""
        self.validate_skill_name(name)
        if not self.custom_skill_exists(name):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        target = self.get_custom_skill_dir(name)
        if target.exists():
            shutil.rmtree(target)

    def append_history(self, name: str, record: dict) -> None:
        """Append a JSONL history entry for *name*."""
        self.validate_skill_name(name)
        payload = {"ts": datetime.now(UTC).isoformat(), **record}
        history_path = self.get_skill_history_file(name)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")

    def read_history(self, name: str) -> list[dict]:
        """Return all history records for *name*, oldest first."""
        self.validate_skill_name(name)
        history_path = self.get_skill_history_file(name)
        if not history_path.exists():
            return []
        records: list[dict] = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records
