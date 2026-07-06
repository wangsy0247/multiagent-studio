"""Skill module — public API surface.

Exports the core types and functions needed by other harness modules.
"""

from .types import SKILL_MD_FILE, Skill, SkillCategory

__all__ = [
    "Skill",
    "SkillCategory",
    "SKILL_MD_FILE",
    "parse_skill_file",
    "SkillStorage",
    "get_skills_prompt_section",
    "filter_tools_by_skill_allowed_tools",
]


# ---------------------------------------------------------------------------
# Lazy imports to avoid circular dependencies with storage
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    if name == "parse_skill_file":
        from .parser import parse_skill_file

        return parse_skill_file
    if name == "SkillStorage":
        from .storage import SkillStorage

        return SkillStorage
    if name == "get_skills_prompt_section":
        from .prompt import get_skills_prompt_section

        return get_skills_prompt_section
    if name == "filter_tools_by_skill_allowed_tools":
        from .tool_policy import filter_tools_by_skill_allowed_tools

        return filter_tools_by_skill_allowed_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
