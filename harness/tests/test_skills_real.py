"""End-to-end test — load real skills from the project skills/ directory."""

import pytest
from pathlib import Path

from harness.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from harness.skills.storage import SkillStorage
from harness.skills.parser import parse_skill_file
from harness.skills.prompt import get_skills_prompt_section
from harness.skills.tool_policy import filter_tools_by_skill_allowed_tools

# Resolve the project skills/ directory relative to this test file.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS_ROOT = _PROJECT_ROOT / "skills"


@pytest.fixture
def storage():
    """A SkillStorage pointed at the project's real skills/ directory."""
    return SkillStorage(_SKILLS_ROOT)


# ===================================================================
# Real skills loading
# ===================================================================


class TestRealSkills:
    """Verify that the skills created in skills/{public,custom}/ load correctly."""

    def test_skills_directory_exists(self):
        assert _SKILLS_ROOT.exists(), f"skills/ directory not found at {_SKILLS_ROOT}"
        assert (_SKILLS_ROOT / "public").is_dir()
        assert (_SKILLS_ROOT / "custom").is_dir()

    def test_load_all_skills(self, storage):
        skills = storage.load_skills()
        assert len(skills) >= 4, f"Expected at least 4 skills, got {len(skills)}"

        names = {s.name for s in skills}
        assert "greeting-responder" in names
        assert "code-reviewer" in names
        assert "deep-research" in names
        assert "my-workflow" in names  # custom skill

    def test_public_skills_are_public(self, storage):
        skills = storage.load_skills()
        public = {s.name: s for s in skills if s.category == SkillCategory.PUBLIC}
        assert "greeting-responder" in public
        assert "code-reviewer" in public
        assert "deep-research" in public

    def test_custom_skills_are_custom(self, storage):
        skills = storage.load_skills()
        custom = {s.name: s for s in skills if s.category == SkillCategory.CUSTOM}
        assert "my-workflow" in custom

    def test_all_skills_enabled_by_default(self, storage):
        skills = storage.load_skills()
        for s in skills:
            assert s.enabled is True, f"Skill '{s.name}' should be enabled by default"

    def test_skills_are_sorted_alphabetically(self, storage):
        skills = storage.load_skills()
        names = [s.name for s in skills]
        assert names == sorted(names), f"Skills not sorted: {names}"


# ===================================================================
# Skill metadata
# ===================================================================


class TestSkillMetadata:
    """Verify that individual skills have correct metadata."""

    def test_greeting_responder_metadata(self, storage):
        skills = {s.name: s for s in storage.load_skills()}
        skill = skills["greeting-responder"]
        assert skill.description.startswith("Respond to user greetings")
        assert skill.license == "MIT"
        assert skill.category == SkillCategory.PUBLIC
        assert skill.allowed_tools is None  # no restriction

    def test_code_reviewer_allowed_tools(self, storage):
        skills = {s.name: s for s in storage.load_skills()}
        skill = skills["code-reviewer"]
        assert skill.allowed_tools == [
            "file_read", "list_files", "grep_tool", "glob_tool"
        ]

    def test_deep_research_allowed_tools(self, storage):
        skills = {s.name: s for s in storage.load_skills()}
        skill = skills["deep-research"]
        assert "web_search" in skill.allowed_tools
        assert "web_fetch" in skill.allowed_tools

    def test_my_workflow_is_custom(self, storage):
        skills = {s.name: s for s in storage.load_skills()}
        skill = skills["my-workflow"]
        assert skill.category == SkillCategory.CUSTOM
        assert skill.description.startswith("My custom daily workflow")
        assert skill.allowed_tools is None  # no tool restriction for custom skill


# ===================================================================
# Prompt section generation
# ===================================================================


class TestPromptGeneration:
    """Verify that the <skill_system> XML block is generated correctly."""

    def test_prompt_section_contains_all_skills(self, storage):
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)

        assert "<skill_system>" in section
        assert "<available_skills>" in section
        assert "<name>greeting-responder</name>" in section
        assert "<name>code-reviewer</name>" in section
        assert "<name>deep-research</name>" in section
        assert "<name>my-workflow</name>" in section

    def test_prompt_section_has_locations(self, storage):
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)

        assert "/mnt/skills/public/greeting-responder/SKILL.md" in section
        assert "/mnt/skills/public/code-reviewer/SKILL.md" in section
        assert "/mnt/skills/public/deep-research/SKILL.md" in section
        assert "/mnt/skills/custom/my-workflow/SKILL.md" in section

    def test_prompt_section_labels_categories(self, storage):
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)

        # Public skills labeled [public], custom labeled [custom]
        assert "[public]" in section
        assert "[custom]" in section

    def test_prompt_length_is_reasonable(self, storage):
        """Prompt section shouldn't be excessively large even with 4 skills."""
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)
        # Should be well under 4096 chars for 4 skills
        assert len(section) < 3000, f"Prompt section too long: {len(section)} chars"


# ===================================================================
# Tool filtering with real skills
# ===================================================================


class TestToolFiltering:
    """Verify tool filtering works with the real skills."""

    def test_code_reviewer_restricts_tools(self):
        from dataclasses import dataclass

        @dataclass
        class FakeTool:
            name: str

        tools = [
            FakeTool("file_read"),
            FakeTool("list_files"),
            FakeTool("grep_tool"),
            FakeTool("glob_tool"),
            FakeTool("bash"),
            FakeTool("web_search"),
            FakeTool("web_fetch"),
        ]

        skills = [
            Skill(
                name="code-reviewer",
                description="Review code",
                license="MIT",
                skill_dir=Path("/fake"),
                skill_file=Path("/fake/SKILL.md"),
                relative_path=Path("code-reviewer"),
                category=SkillCategory.PUBLIC,
                allowed_tools=["file_read", "list_files", "grep_tool", "glob_tool"],
                enabled=True,
            )
        ]

        filtered = filter_tools_by_skill_allowed_tools(tools, skills)
        names = {t.name for t in filtered}
        assert names == {"file_read", "list_files", "grep_tool", "glob_tool"}
        assert "bash" not in names
        assert "web_search" not in names

    def test_mixed_skills_union_tools(self):
        from dataclasses import dataclass

        @dataclass
        class FakeTool:
            name: str

        tools = [
            FakeTool("file_read"),
            FakeTool("web_search"),
            FakeTool("bash"),
        ]

        skills = [
            Skill(
                name="code-reviewer",
                description="Review code",
                license="MIT",
                skill_dir=Path("/fake1"),
                skill_file=Path("/fake1/SKILL.md"),
                relative_path=Path("code-reviewer"),
                category=SkillCategory.PUBLIC,
                allowed_tools=["file_read"],
                enabled=True,
            ),
            Skill(
                name="deep-research",
                description="Research",
                license="MIT",
                skill_dir=Path("/fake2"),
                skill_file=Path("/fake2/SKILL.md"),
                relative_path=Path("deep-research"),
                category=SkillCategory.PUBLIC,
                allowed_tools=["web_search"],
                enabled=True,
            ),
        ]

        filtered = filter_tools_by_skill_allowed_tools(tools, skills)
        names = {t.name for t in filtered}
        assert names == {"file_read", "web_search"}
        assert "bash" not in names  # not in any skill's allowed list
