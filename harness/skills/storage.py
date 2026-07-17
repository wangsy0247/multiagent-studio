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
    """Local-filesystem skill storage with per-user isolation.

    Directory layout::

        <root>/                        ← project-level skills
        ├── public/<name>/SKILL.md     ← built-in (git-tracked, read-only)
        ├── custom/<name>/SKILL.md     ← shared custom (.gitignored, editable)
        └── custom/.history/<name>.jsonl

        <user_skills_base>/            ← per-user skills (optional)
        └── {user_id}/
            └── skills/<name>/SKILL.md

    When *sandbox_sync_root* is set, every project-skill write / delete also
    mirrors the change into that directory so Docker / OpenSandbox containers
    can access the latest skill files.  User skills live under the data root
    already (via *user_skills_base*) and don't need separate syncing.
    """

    def __init__(
        self,
        root_path: Path,
        container_path: str = "/mnt/skills",
        sandbox_sync_root: Path | None = None,
        user_skills_base: Path | None = None,
    ) -> None:
        self._root = root_path
        self._container_root = container_path
        self._sandbox_sync_root = sandbox_sync_root
        self._user_skills_base = user_skills_base  # {base}/users/

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

    def _sync_to_sandbox(self, name: str) -> None:
        """Copy a custom skill from the primary root to the sandbox sync root.

        Uses ``copyfile`` + ``chmod`` instead of ``copy2`` to avoid
        preserving host UID/GID (container may not have matching user).
        """
        if self._sandbox_sync_root is None:
            return
        import shutil

        src_dir = self.get_custom_skill_dir(name)
        if not src_dir.exists():
            return
        dst_dir = self._sandbox_sync_root / SkillCategory.CUSTOM.value / name
        # Remove stale destination before copy (handles deletes).
        if dst_dir.exists():
            shutil.rmtree(dst_dir)

        def _copy_tree(src: Path, dst: Path) -> None:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if item.is_dir():
                    _copy_tree(item, dst / item.name)
                else:
                    dst_file = dst / item.name
                    shutil.copyfile(item, dst_file)
                    dst_file.chmod(0o644)

        _copy_tree(src_dir, dst_dir)

    def _remove_from_sandbox(self, name: str) -> None:
        """Remove a custom skill from the sandbox sync root."""
        if self._sandbox_sync_root is None:
            return
        dst_dir = self._sandbox_sync_root / SkillCategory.CUSTOM.value / name
        if dst_dir.exists():
            import shutil
            shutil.rmtree(dst_dir)

    def get_custom_skill_dir(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to a custom skill directory (project-shared or user-private)."""
        if user_id:
            return self.get_user_skill_dir(user_id, name)
        normalized = self.validate_skill_name(name)
        return self._root / SkillCategory.CUSTOM.value / normalized

    def get_custom_skill_file(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to ``SKILL.md`` for a custom skill."""
        return self.get_custom_skill_dir(name, user_id=user_id) / SKILL_MD_FILE

    def get_skill_history_file(
        self, name: str, *, user_id: str | None = None,
    ) -> Path:
        """Path to ``.history/<name>.jsonl`` for a custom skill.

        History lives at the category level (e.g. ``custom/.history/`` or
        ``users/<uid>/skills/.history/``) — NOT inside the skill directory —
        so that deleting a skill does not wipe its history trail.
        """
        normalized = self.validate_skill_name(name)
        if user_id:
            if self._user_skills_base is None:
                raise RuntimeError("user_skills_base is not configured")
            return (
                self._user_skills_base / user_id / "skills"
                / ".history"
                / f"{normalized}.jsonl"
            )
        return (
            self._root
            / SkillCategory.CUSTOM.value
            / ".history"
            / f"{normalized}.jsonl"
        )

    def custom_skill_exists(
        self, name: str, *, user_id: str | None = None,
    ) -> bool:
        return self.get_custom_skill_file(name, user_id=user_id).exists()

    def public_skill_exists(self, name: str) -> bool:
        return (
            self._root / SkillCategory.PUBLIC.value / name / SKILL_MD_FILE
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
        same-named project skills.

        Re-reads ``extensions_config.json`` on every call so external changes
        (e.g. made by another process or the frontend) are picked up immediately.
        """
        skills_by_name: dict[str, Skill] = {}

        # 1. Discover and parse project SKILL.md files.
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

        # 2. Discover and parse user-private SKILL.md files.
        if user_id:
            for user_root, md_path in self._iter_user_skill_files(user_id):
                skill = parse_skill_file(
                    md_path,
                    category=SkillCategory.CUSTOM,
                    relative_path=md_path.parent.relative_to(user_root),
                )
                if skill is None:
                    continue
                skill.user_id = user_id
                # User skills override same-named project skills.
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
        """Atomically write a text file under the custom skill directory.

        Uses tempfile + rename to guarantee atomicity.
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

        # Mirror to sandbox-accessible location (project skills only —
        # user skills are already under the data root).
        if user_id is None:
            self._sync_to_sandbox(name)

    def delete_custom_skill(
        self, name: str, *, user_id: str | None = None,
    ) -> None:
        """Delete a custom skill directory entirely."""
        self.validate_skill_name(name)
        if not self.custom_skill_exists(name, user_id=user_id):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        target = self.get_custom_skill_dir(name, user_id=user_id)
        if target.exists():
            shutil.rmtree(target)

        # Remove from sandbox mirror (project skills only).
        if user_id is None:
            self._remove_from_sandbox(name)

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
