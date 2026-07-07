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
    "scan_skill_content",
    "ScanDecision",
    "ScanResult",
    "refresh_skills_system_prompt_cache",
    "get_cached_skills_prompt_section",
    "install_skill_from_archive",
    "ensure_safe_support_path",
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
    if name == "scan_skill_content":
        from .security_scanner import scan_skill_content
        return scan_skill_content
    if name in ("ScanDecision", "ScanResult"):
        from . import security_scanner
        return getattr(security_scanner, name)
    if name == "refresh_skills_system_prompt_cache":
        from .cache import refresh_skills_system_prompt_cache
        return refresh_skills_system_prompt_cache
    if name == "get_cached_skills_prompt_section":
        from .cache import get_cached_skills_prompt_section
        return get_cached_skills_prompt_section
    if name == "install_skill_from_archive":
        from .installer import install_skill_from_archive
        return install_skill_from_archive
    if name == "ensure_safe_support_path":
        from .installer import ensure_safe_support_path
        return ensure_safe_support_path
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
