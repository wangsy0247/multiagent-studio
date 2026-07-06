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
        "compatibility",
        "version",
        "author",
    }
)

# Skill names: lowercase letters, digits, and single hyphens as separators.
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024


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
    if not _NAME_PATTERN.fullmatch(name):
        return (
            False,
            f"Skill name '{name}' must use lowercase letters, digits, and hyphens "
            f"(e.g. 'my-skill-name')",
            None,
        )
    if len(name) > _MAX_NAME_LENGTH:
        return (
            False,
            f"Skill name must be {_MAX_NAME_LENGTH} characters or fewer "
            f"(got {len(name)})",
            None,
        )

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
