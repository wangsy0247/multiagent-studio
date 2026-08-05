"""Skill data types and constants — portable from harness.

A Skill is a self-contained workflow packaged as a directory containing a
``SKILL.md`` file with YAML frontmatter and Markdown instructions.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SKILL_MD_FILE = "SKILL.md"


class SkillCategory(StrEnum):
    """Source category for a skill.

    - ``BUILTIN``: built-in skill bundled with the platform, read-only.
    """

    BUILTIN = "builtin"


@dataclass
class Skill:
    """Represents a skill with its metadata and filesystem paths."""

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path  # Relative path from category root to skill directory
    category: SkillCategory  # 'builtin'
    allowed_tools: list[str] | None = None
    enabled: bool = False  # Whether this skill is enabled
    user_id: str | None = None  # None → project skill; "alice" → user private skill

    @property
    def skill_path(self) -> str:
        """Returns the relative POSIX path from the category root."""
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        """Get the full path to this skill directory in the sandbox container.

        User-private skills live under ``/mnt/skills/my/``.
        Built-in skills live under ``/mnt/skills/builtin/``.

        Args:
            container_base_path: Base path where skills are mounted.

        Returns:
            Full container path to the skill directory.
        """
        if self.user_id:
            return f"{container_base_path}/my/{self.relative_path}"
        return f"{container_base_path}/builtin/{self.relative_path}"

    def get_container_file_path(self, container_base_path: str = "/mnt/skills") -> str:
        """Get the full path to this skill's SKILL.md in the sandbox container."""
        return f"{self.get_container_path(container_base_path)}/SKILL.md"

    def __repr__(self) -> str:
        return (
            f"Skill(name={self.name!r}, description={self.description!r}, "
            f"category={self.category!r})"
        )
