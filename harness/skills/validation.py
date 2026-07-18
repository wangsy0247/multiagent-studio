"""SKILL.md frontmatter validation — portable from DeerFlow.

Pure validation logic with no HTTP/FastAPI dependencies.
"""

import re
from pathlib import Path

import yaml

from .parser import parse_allowed_tools
from .types import SKILL_MD_FILE

# Only these frontmatter keys are recognised; unknown keys are rejected.
ALLOWED_FRONTMATTER_PROPERTIES: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "version",
        "author",
    }
)

# Skill names: lowercase letters, digits, and single hyphens as separators.
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024


def validate_skill_name(name: str) -> str:
    """Validate and normalise a skill name; return the normalised form.

    Shared by ``SkillStorage`` and ``_validate_skill_frontmatter`` so the
    rules are defined in exactly one place.

    Raises:
        ValueError: Name does not match the required convention.
    """
    normalized = name.strip()
    if not _NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Skill name must be hyphen-case using lowercase letters, "
            "digits, and hyphens only."
        )
    if len(normalized) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"Skill name must be {_MAX_NAME_LENGTH} characters or fewer "
            f"(got {len(normalized)})."
        )
    return normalized


def _validate_skill_frontmatter(skill_dir: Path) -> tuple[bool, str, str | None]:
    """Validate the SKILL.md frontmatter in *skill_dir*.

    Returns:
        ``(is_valid, message, skill_name)`` tuple.  ``skill_name`` is only
        populated when the validation passes.
    """
    skill_file = skill_dir / SKILL_MD_FILE

    if not skill_file.exists():
        return False, f"SKILL.md not found in {skill_dir}", None

    content = skill_file.read_text(encoding="utf-8")

    # --- frontmatter extraction -------------------------------------------------
    frontmatter_re = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
    m = frontmatter_re.match(content)
    if not m:
        return False, "SKILL.md must start with YAML frontmatter between --- fences", None

    try:
        metadata = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}", None

    if not isinstance(metadata, dict):
        return False, "Frontmatter must be a YAML mapping (key-value pairs)", None

    # --- unknown properties ------------------------------------------------------
    unknown_keys = set(metadata) - ALLOWED_FRONTMATTER_PROPERTIES
    if unknown_keys:
        return (
            False,
            f"Unknown frontmatter properties: {', '.join(sorted(unknown_keys))}",
            None,
        )

    # --- required fields ---------------------------------------------------------
    name = metadata.get("name")
    description = metadata.get("description")

    if not name or not isinstance(name, str) or not name.strip():
        return False, "Frontmatter must contain a non-empty 'name' string", None
    if not description or not isinstance(description, str) or not description.strip():
        return False, "Frontmatter must contain a non-empty 'description' string", None

    name = name.strip()
    description = description.strip()

    # --- name validation ---------------------------------------------------------
    try:
        name = validate_skill_name(name)
    except ValueError as e:
        return False, str(e), None

    # --- description validation --------------------------------------------------
    if "<" in description or ">" in description:
        return False, "Description must not contain angle brackets (< or >)", None
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        return (
            False,
            f"Description must be {_MAX_DESCRIPTION_LENGTH} characters or fewer "
            f"(got {len(description)})",
            None,
        )

    # --- allowed-tools validation ------------------------------------------------
    try:
        parse_allowed_tools(metadata.get("allowed-tools"), skill_file)
    except ValueError as e:
        return False, str(e), None

    return True, "OK", name
