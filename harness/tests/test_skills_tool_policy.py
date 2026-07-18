"""Tests for harness/skills/tool_policy.py — skill-based tool filtering."""

import pytest
from dataclasses import dataclass
from pathlib import Path

from harness.skills.types import Skill, SkillCategory
from harness.skills.tool_policy import (
    allowed_tool_names_for_skills,
    filter_tools_by_skill_allowed_tools,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTool:
    """Minimal tool stub implementing the NamedTool protocol."""
    name: str


def _make_skill(
    name: str = "test-skill",
    description: str = "A test skill",
    allowed_tools: list[str] | None = None,
    category: SkillCategory = SkillCategory.BUILTIN,
    enabled: bool = True,
) -> Skill:
    return Skill(
        name=name,
        description=description,
        license=None,
        skill_dir=Path(f"/fake/{name}"),
        skill_file=Path(f"/fake/{name}/SKILL.md"),
        relative_path=Path(name),
        category=category,
        allowed_tools=allowed_tools,
        enabled=enabled,
    )


def _make_tools(*names: str) -> list[FakeTool]:
    return [FakeTool(name=n) for n in names]


# ===================================================================
# allowed_tool_names_for_skills
# ===================================================================


class TestAllowedToolNamesForSkills:
    def test_no_skills_returns_none(self):
        assert allowed_tool_names_for_skills([]) is None

    def test_skills_without_allowed_tools_returns_none(self):
        """Skills that don't declare allowed-tools → legacy allow-all."""
        skills = [_make_skill("a"), _make_skill("b")]
        assert allowed_tool_names_for_skills(skills) is None

    def test_empty_allowed_tools_contributes_nothing(self):
        """Explicit empty list → contributes no tools but triggers filtering."""
        skills = [_make_skill("restricted", allowed_tools=[])]
        result = allowed_tool_names_for_skills(skills)
        assert result == set()

    def test_single_skill_allowed_tools(self):
        skills = [_make_skill("a", allowed_tools=["bash", "read_file"])]
        result = allowed_tool_names_for_skills(skills)
        assert result == {"bash", "read_file"}

    def test_union_across_skills(self):
        skills = [
            _make_skill("a", allowed_tools=["bash", "read_file"]),
            _make_skill("b", allowed_tools=["web_search"]),
        ]
        result = allowed_tool_names_for_skills(skills)
        assert result == {"bash", "read_file", "web_search"}

    def test_mixed_declared_and_undeclared(self):
        """Skills without allowed-tools contribute nothing when others declare."""
        skills = [
            _make_skill("a", allowed_tools=["bash"]),
            _make_skill("b", allowed_tools=None),  # legacy — contributes nothing
        ]
        result = allowed_tool_names_for_skills(skills)
        assert result == {"bash"}  # skill-b's None is ignored


# ===================================================================
# filter_tools_by_skill_allowed_tools
# ===================================================================


class TestFilterToolsBySkillAllowedTools:
    def test_no_skills_no_filtering(self):
        tools = _make_tools("bash", "read_file", "web_search")
        result = filter_tools_by_skill_allowed_tools(tools, [])
        assert len(result) == 3

    def test_skills_without_allowed_tools_no_filtering(self):
        """Legacy skills (no allowed-tools field) → allow all tools."""
        tools = _make_tools("bash", "read_file", "web_search")
        skills = [_make_skill("a")]
        result = filter_tools_by_skill_allowed_tools(tools, skills)
        assert len(result) == 3

    def test_empty_allowed_tools_filters_all(self):
        """Skill with empty allowed-tools → no tools pass."""
        tools = _make_tools("bash", "read_file")
        skills = [_make_skill("restricted", allowed_tools=[])]
        result = filter_tools_by_skill_allowed_tools(tools, skills)
        assert result == []

    def test_allowed_tools_filters_correctly(self):
        tools = _make_tools("bash", "read_file", "web_search", "task")
        skills = [_make_skill("a", allowed_tools=["bash", "read_file"])]
        result = filter_tools_by_skill_allowed_tools(tools, skills)
        assert {t.name for t in result} == {"bash", "read_file"}

    def test_tool_not_in_allowed_is_removed(self):
        tools = _make_tools("bash", "dangerous_tool")
        skills = [_make_skill("a", allowed_tools=["bash"])]
        result = filter_tools_by_skill_allowed_tools(tools, skills)
        names = {t.name for t in result}
        assert "bash" in names
        assert "dangerous_tool" not in names

    def test_union_of_multiple_skills(self):
        tools = _make_tools("bash", "read_file", "web_search", "task")
        skills = [
            _make_skill("a", allowed_tools=["bash"]),
            _make_skill("b", allowed_tools=["web_search", "task"]),
        ]
        result = filter_tools_by_skill_allowed_tools(tools, skills)
        assert {t.name for t in result} == {"bash", "web_search", "task"}

    def test_disabled_skills_should_not_filter(self):
        """Disabled skills should be excluded before calling filter."""
        tools = _make_tools("bash", "read_file")
        all_skills = [
            _make_skill("a", allowed_tools=["bash"]),
            _make_skill("b", allowed_tools=[]),  # would block all, but disabled
        ]
        # Simulate what SkillStorage.load_skills(enabled_only=True) does:
        enabled = [s for s in all_skills if s.enabled]
        # Currently both are enabled, filter normally
        result_all = filter_tools_by_skill_allowed_tools(tools, enabled)
        assert {t.name for t in result_all} == {"bash"}

        # Now disable skill-b:
        all_skills[1].enabled = False
        enabled = [s for s in all_skills if s.enabled]
        result_enabled_only = filter_tools_by_skill_allowed_tools(tools, enabled)
        # Only skill-a's allowed-tools apply
        assert {t.name for t in result_enabled_only} == {"bash"}
