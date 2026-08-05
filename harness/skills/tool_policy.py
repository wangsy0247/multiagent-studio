"""Tool filtering based on skill ``allowed-tools`` declarations — portable from harness.

When one or more enabled skills declare ``allowed-tools``, the lead agent's
tool set is restricted to the union of all declared tool names.  Skills
without the field (``None``) are treated as legacy and do not restrict tools.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    """Structural protocol for any object with a ``name: str`` attribute."""

    name: str


def allowed_tool_names_for_skills(skills: list[Skill]) -> set[str] | None:
    """Return the union of explicit skill ``allowed-tools`` declarations.

    Returns ``None`` when **no** loaded skill declares ``allowed-tools``
    (legacy allow-all behaviour).  Returns a ``set[str]`` (possibly empty)
    once at least one skill declares the field — skills without the field
    contribute **no** tools when an explicit declaration exists elsewhere.
    """
    if not skills:
        return None

    allowed: set[str] = set()
    has_explicit_declaration = False

    for skill in skills:
        if skill.allowed_tools is None:
            continue
        has_explicit_declaration = True
        if not skill.allowed_tools:
            logger.info("Skill %s declared empty allowed-tools", skill.name)
        allowed.update(skill.allowed_tools)

    if not has_explicit_declaration:
        return None  # legacy: allow all tools
    return allowed


def filter_tools_by_skill_allowed_tools[ToolT: NamedTool](
    tools: list[ToolT],
    skills: list[Skill],
) -> list[ToolT]:
    """Filter *tools* to only those permitted by the active skills.

    When no skill declares ``allowed-tools`` the original list is returned
    unchanged.
    """
    allowed = allowed_tool_names_for_skills(skills)
    if allowed is None:
        return tools  # no declarations — allow all
    return [tool for tool in tools if tool.name in allowed]
