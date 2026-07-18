"""Integration tests — skills in system prompt and LeadAgent wiring."""

import pytest
from pathlib import Path

from harness.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from harness.skills.storage import SkillStorage
from harness.skills.parser import parse_skill_file
from harness.skills.prompt import get_skills_prompt_section


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_skill_md(
    skill_dir: Path, name: str, description: str = "A test skill"
) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    md_path = skill_dir / SKILL_MD_FILE
    md_path.write_text(content, encoding="utf-8")
    return md_path


def _make_storage(tmp_path: Path) -> SkillStorage:
    root = tmp_path / "skills"
    (root / "builtin").mkdir(parents=True, exist_ok=True)
    return SkillStorage(root)


# ===================================================================
# get_skills_prompt_section
# ===================================================================


class TestGetSkillsPromptSection:
    def test_empty_skills_returns_empty_string(self):
        assert get_skills_prompt_section([]) == ""

    def test_single_skill_in_section(self, tmp_path):
        skill_dir = tmp_path / "test-skill"
        md = _write_skill_md(skill_dir, "test-skill", "Does testing things")
        skill = parse_skill_file(md, SkillCategory.BUILTIN)
        assert skill is not None

        section = get_skills_prompt_section([skill])
        assert "<skill_system>" in section
        assert "<name>test-skill</name>" in section
        assert "Does testing things" in section
        assert "[built-in]" in section
        assert "/mnt/skills/builtin/test-skill/SKILL.md" in section

    def test_multiple_skills(self, tmp_path):
        skills = []
        for name in ["skill-a", "skill-b"]:
            skill_dir = tmp_path / name
            md = _write_skill_md(skill_dir, name)
            s = parse_skill_file(md, SkillCategory.BUILTIN)
            assert s is not None
            skills.append(s)

        section = get_skills_prompt_section(skills)
        assert "<name>skill-a</name>" in section
        assert "<name>skill-b</name>" in section

    def test_builtin_skill_labeled(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        md = _write_skill_md(skill_dir, "my-skill")
        skill = parse_skill_file(md, SkillCategory.BUILTIN)
        assert skill is not None

        section = get_skills_prompt_section([skill])
        assert "[built-in]" in section
        assert "/mnt/skills/builtin/my-skill/SKILL.md" in section

    def test_custom_container_path(self, tmp_path):
        skill_dir = tmp_path / "test-skill"
        md = _write_skill_md(skill_dir, "test-skill")
        skill = parse_skill_file(md, SkillCategory.BUILTIN)
        assert skill is not None

        section = get_skills_prompt_section([skill], container_base_path="/opt/skills")
        assert "/opt/skills/builtin/test-skill/SKILL.md" in section

    def test_nested_skill_container_path(self, tmp_path):
        nested = tmp_path / "subdir" / "nested-skill"
        md = _write_skill_md(nested, "nested-skill")
        skill = parse_skill_file(
            md, SkillCategory.BUILTIN, relative_path=Path("subdir/nested-skill")
        )
        assert skill is not None

        section = get_skills_prompt_section([skill])
        assert "/mnt/skills/builtin/subdir/nested-skill/SKILL.md" in section


# ===================================================================
# SkillStorage → get_skills_prompt_section round-trip
# ===================================================================


class TestStorageToPromptRoundTrip:
    """Verify that skills loaded from storage produce valid prompt sections."""

    def test_load_and_generate_prompt(self, tmp_path):
        storage = _make_storage(tmp_path)
        _write_skill_md(
            storage._root / "builtin" / "deep-research",
            "deep-research",
            "Multi-source research with web search and citations",
        )
        _write_skill_md(
            storage._root / "builtin" / "my-workflow",
            "my-workflow",
            "Custom workflow for my project",
        )

        skills = storage.load_skills(enabled_only=True)
        assert len(skills) == 2

        section = get_skills_prompt_section(skills)
        assert "<name>deep-research</name>" in section
        assert "<name>my-workflow</name>" in section
        assert "[built-in]" in section
        assert "Multi-source research" in section
        assert "Custom workflow" in section

    def test_enabled_only_skills_in_prompt(self, tmp_path):
        """Only enabled skills appear in the prompt section."""
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "skill-a", "skill-a")
        _write_skill_md(storage._root / "builtin" / "skill-b", "skill-b")

        all_skills = storage.load_skills()
        assert len(all_skills) == 2

        # Simulate one skill disabled
        for s in all_skills:
            if s.name == "skill-b":
                s.enabled = False

        enabled = [s for s in all_skills if s.enabled]
        section = get_skills_prompt_section(enabled)
        assert "<name>skill-a</name>" in section
        assert "<name>skill-b</name>" not in section

    def test_no_skills_produces_empty_prompt(self, tmp_path):
        storage = _make_storage(tmp_path)
        skills = storage.load_skills()
        assert skills == []
        assert get_skills_prompt_section(skills) == ""
