"""Local-filesystem skill storage — discovery, loading, and CRUD.

Two-tier directory layout::

    <root>/                       ← project-level built-in skills
    └── builtin/<name>/SKILL.md   ← platform-builtin (git-tracked, read-only)

    <user_skills_base>/           ← per-user skills
    └── {user_id}/
        └── skills/
            ├── <name>/SKILL.md
            └── .history/<name>.jsonl

All user-created skills go into per-user directories. No shared ``custom/``
directory — each user's skills are private and managed via ``skill_manage``.
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
from .validation import validate_skill_name

logger = logging.getLogger(__name__)


class SkillStorage:
    """Local-filesystem skill storage with per-user isolation.

    Directory layout::

        <root>/                        ← project-level built-in skills
        └── builtin/<name>/SKILL.md    ← platform-builtin (git-tracked, read-only)

        <user_skills_base>/            ← per-user skills
        └── {user_id}/
            └── skills/
                ├── <name>/SKILL.md
                └── .history/<name>.jsonl

    User-created skills are per-user private — no shared ``custom/`` directory.
    Built-in skills are mounted directly from the project directory by the
    sandbox (see ``local_sandbox_provider.py``).
    """

    def __init__(
        self,
        root_path: Path,
        container_path: str = "/mnt/skills",
        user_skills_base: Path | None = None,
    ) -> None:
        self._root = root_path
        self._container_root = container_path
        self._user_skills_base = user_skills_base  # {base}/users/

    # ------------------------------------------------------------------
    # static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def validate_skill_name(name: str) -> str:
        """Validate and normalise a skill name; return the normalised form.

        Delegates to ``harness.skills.validation.validate_skill_name`` —
        the single source of truth for skill name rules.
        """
        return validate_skill_name(name)

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def get_skills_root_path(self) -> Path:
        """Host path to the skills root (used for sandbox mounts)."""
        return self._root

    def get_container_root(self) -> str:
        """Container path where skills are mounted (e.g. ``/mnt/skills``)."""
        return self._container_root

    def get_custom_skill_dir(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to a user-private skill directory.

        Raises ValueError when *user_id* is None (shared skills no longer exist).
        """
        if not user_id:
            raise ValueError(
                "user_id is required for skill operations. "
                "Shared custom skills have been removed — use per-user skills."
            )
        return self.get_user_skill_dir(user_id, name)

    def get_custom_skill_file(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to ``SKILL.md`` for a custom skill."""
        return self.get_custom_skill_dir(name, user_id=user_id) / SKILL_MD_FILE

    def get_skill_history_file(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to ``.history/<name>.jsonl`` for a user skill.

        History lives at the user skills level — NOT inside the skill directory —
        so that deleting a skill does not wipe its history trail.
        """
        if not user_id:
            raise ValueError("user_id is required for skill history")
        normalized = self.validate_skill_name(name)
        if self._user_skills_base is None:
            raise RuntimeError("user_skills_base is not configured")
        return (
            self._user_skills_base / user_id / "skills"
            / ".history"
            / f"{normalized}.jsonl"
        )

    def custom_skill_exists(
        self, name: str, *, user_id: str | None = None,
    ) -> bool:
        return self.get_custom_skill_file(name, user_id=user_id).exists()

    def builtin_skill_exists(self, name: str) -> bool:
        return (
            self._root / SkillCategory.BUILTIN.value / name / SKILL_MD_FILE
        ).exists()

    # ------------------------------------------------------------------
    # user skill paths
    # ------------------------------------------------------------------

    def get_user_skill_dir(self, user_id: str, name: str) -> Path:
        """Path to ``{user_skills_base}/{uid}/skills/{name}``."""
        normalized = self.validate_skill_name(name)
        if self._user_skills_base is None:
            raise RuntimeError("user_skills_base is not configured")
        return self._user_skills_base / user_id / "skills" / normalized

    def user_skill_exists(self, user_id: str, name: str) -> bool:
        """Check whether a user-private skill exists."""
        if self._user_skills_base is None:
            return False
        return (self.get_user_skill_dir(user_id, name) / SKILL_MD_FILE).exists()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
        """Yield ``(category, category_root, skill_md_path)`` for built-in SKILL.md files."""
        if not self._root.exists():
            return
        category = SkillCategory.BUILTIN
        category_path = self._root / category.value
        if not category_path.exists() or not category_path.is_dir():
            return
        for current_root, dir_names, file_names in os.walk(
            category_path, followlinks=True
        ):
            dir_names[:] = sorted(
                name for name in dir_names if not name.startswith(".")
            )
            if SKILL_MD_FILE not in file_names:
                continue
            yield category, category_path, Path(current_root) / SKILL_MD_FILE

    def _iter_user_skill_files(
        self, user_id: str,
    ) -> Iterable[tuple[Path, Path]]:
        """Yield ``(category_root, skill_md_path)`` for each user-private SKILL.md."""
        if self._user_skills_base is None:
            return
        user_root = self._user_skills_base / user_id / "skills"
        if not user_root.exists() or not user_root.is_dir():
            return
        for current_root, dir_names, file_names in os.walk(
            user_root, followlinks=True
        ):
            dir_names[:] = sorted(
                name for name in dir_names if not name.startswith(".")
            )
            if SKILL_MD_FILE not in file_names:
                continue
            yield user_root, Path(current_root) / SKILL_MD_FILE

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def load_skills(
        self, *, enabled_only: bool = False, user_id: str | None = None,
    ) -> list[Skill]:
        """Discover all skills, merge enabled state, sort, and optionally filter.

        When *user_id* is provided, user-private skills from
        ``<user_skills_base>/<uid>/skills/`` are also loaded and tagged with
        ``user_id`` on the ``Skill`` object.  User skills take precedence over
        same-named built-in skills.

        Re-reads ``extensions_config.json`` on every call so external changes
        (e.g. made by another process or the frontend) are picked up immediately.
        """
        skills_by_name: dict[str, Skill] = {}

        # 1. Discover and parse built-in SKILL.md files.
        for category, category_root, md_path in self._iter_skill_files():
            skill = parse_skill_file(
                md_path,
                category=category,
                relative_path=md_path.parent.relative_to(category_root),
            )
            if skill is None:
                continue
            skills_by_name[skill.name] = skill

        # 2. Discover and parse user-private SKILL.md files.
        if user_id:
            for user_root, md_path in self._iter_user_skill_files(user_id):
                skill = parse_skill_file(
                    md_path,
                    category=SkillCategory.BUILTIN,
                    relative_path=md_path.parent.relative_to(user_root),
                )
                if skill is None:
                    continue
                skill.user_id = user_id
                # User skills override same-named built-in skills.
                skills_by_name[skill.name] = skill

        skills = list(skills_by_name.values())

        # 3. Merge enabled state from extensions_config.json.
        try:
            from harness.config.extensions_config import ExtensionsConfig

            extensions_config = ExtensionsConfig.from_file()
            for skill in skills:
                skill.enabled = extensions_config.is_skill_enabled(
                    skill.name, skill.category
                )
        except Exception as exc:
            logger.warning("Failed to load extensions config: %s", exc)

        # 4. Filter and sort.
        if enabled_only:
            skills = [s for s in skills if s.enabled]
        skills.sort(key=lambda s: s.name)
        return skills

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def read_custom_skill(
        self, name: str, *, user_id: str | None = None,
    ) -> str:
        """Read the SKILL.md content for a custom skill."""
        if not self.custom_skill_exists(name, user_id=user_id):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        return (
            self.get_custom_skill_dir(name, user_id=user_id) / SKILL_MD_FILE
        ).read_text(encoding="utf-8")

    def write_custom_skill(
        self, name: str, relative_path: str, content: str,
        *, user_id: str | None = None,
    ) -> None:
        """Atomically write a text file under the user skill directory.

        Uses tempfile + rename to guarantee atomicity.  User skills are already
        under the data root so no separate sandbox sync is needed.
        """
        target_dir = self.get_custom_skill_dir(name, user_id=user_id)
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

    def delete_custom_skill(
        self, name: str, *, user_id: str | None = None,
    ) -> None:
        """Delete a user skill directory entirely."""
        self.validate_skill_name(name)
        if not self.custom_skill_exists(name, user_id=user_id):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        target = self.get_custom_skill_dir(name, user_id=user_id)
        if target.exists():
            shutil.rmtree(target)

    def append_history(
        self, name: str, record: dict, *, user_id: str | None = None,
    ) -> None:
        """Append a JSONL history entry for *name*."""
        self.validate_skill_name(name)
        payload = {"ts": datetime.now(UTC).isoformat(), **record}
        history_path = self.get_skill_history_file(name, user_id=user_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")

    def read_history(
        self, name: str, *, user_id: str | None = None,
    ) -> list[dict]:
        """Return all history records for *name*, oldest first."""
        self.validate_skill_name(name)
        history_path = self.get_skill_history_file(name, user_id=user_id)
        if not history_path.exists():
            return []
        records: list[dict] = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records
