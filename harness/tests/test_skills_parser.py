"""Tests for harness/skills/parser.py — SKILL.md YAML frontmatter parsing."""

import pytest
from pathlib import Path

from harness.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from harness.skills.parser import parse_skill_file, parse_allowed_tools


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_skill_md(skill_dir: Path, content: str) -> Path:
    """Write SKILL.md with *content*, return its path."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / SKILL_MD_FILE
    md_path.write_text(content, encoding="utf-8")
    return md_path


# ===================================================================
# parse_allowed_tools
# ===================================================================


class TestParseAllowedTools:
    def test_none_returns_none(self):
        assert parse_allowed_tools(None, Path("dummy")) is None

    def test_empty_list_returns_empty_list(self):
        result = parse_allowed_tools([], Path("dummy"))
        assert result == []

    def test_valid_tool_names(self):
        result = parse_allowed_tools(["bash", "read_file"], Path("dummy"))
        assert result == ["bash", "read_file"]

    def test_strips_whitespace(self):
        result = parse_allowed_tools(["  bash  ", " read_file "], Path("dummy"))
        assert result == ["bash", "read_file"]

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            parse_allowed_tools("bash", Path("dummy"))

    def test_rejects_non_string_items(self):
        with pytest.raises(ValueError, match="contain only strings"):
            parse_allowed_tools(["bash", 123], Path("dummy"))

    def test_rejects_empty_tool_name(self):
        with pytest.raises(ValueError, match="cannot contain empty tool names"):
            parse_allowed_tools(["bash", "  "], Path("dummy"))


# ===================================================================
# parse_skill_file
# ===================================================================


class TestParseSkillFile:
    def test_valid_skill(self, tmp_path):
        content = """---
name: test-skill
description: A test skill for unit testing
license: MIT
allowed-tools:
  - bash
  - read_file
---
# Test Skill

This is the body of the skill.
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)

        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill for unit testing"
        assert skill.license == "MIT"
        assert skill.allowed_tools == ["bash", "read_file"]
        assert skill.category == SkillCategory.PUBLIC
        assert skill.enabled is True
        assert skill.skill_file == md_path
        assert skill.skill_dir == tmp_path

    def test_minimal_frontmatter(self, tmp_path):
        content = """---
name: minimal
description: Just the required fields
---
Body here.
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.CUSTOM)

        assert skill is not None
        assert skill.name == "minimal"
        assert skill.description == "Just the required fields"
        assert skill.license is None
        assert skill.allowed_tools is None
        assert skill.category == SkillCategory.CUSTOM

    def test_missing_name(self, tmp_path):
        content = """---
description: No name here
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_missing_description(self, tmp_path):
        content = """---
name: no-desc
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_no_frontmatter(self, tmp_path):
        content = """# Just a markdown file

No YAML frontmatter here.
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_invalid_yaml_frontmatter(self, tmp_path):
        content = """---
name: [unclosed
description: Bad YAML
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_frontmatter_not_a_dict(self, tmp_path):
        content = """---
- just
- a
- list
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_missing_file(self, tmp_path):
        md_path = tmp_path / "nonexistent" / SKILL_MD_FILE
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_wrong_filename(self, tmp_path):
        not_md = tmp_path / "README.md"
        not_md.write_text("---\nname: test\ndescription: desc\n---\n")
        skill = parse_skill_file(not_md, SkillCategory.PUBLIC)
        assert skill is None

    def test_empty_name_string(self, tmp_path):
        content = """---
name: "   "
description: Some description
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_empty_description_string(self, tmp_path):
        content = """---
name: test-skill
description: ""
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_empty_allowed_tools(self, tmp_path):
        """Empty allowed-tools list should be preserved (explicit no-tool skill)."""
        content = """---
name: no-tools-skill
description: This skill explicitly allows no tools
allowed-tools: []
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)

        assert skill is not None
        assert skill.allowed_tools == []

    def test_invalid_allowed_tools_rejected(self, tmp_path):
        content = """---
name: bad-tools
description: Has bad allowed-tools
allowed-tools: not-a-list
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is None

    def test_nested_in_subdirectory(self, tmp_path):
        """Skills in subdirectories should be discovered with correct relative_path."""
        nested = tmp_path / "subdir" / "nested-skill"
        content = """---
name: nested-skill
description: A skill in a subdirectory
---
"""
        md_path = _write_skill_md(nested, content)
        category_root = tmp_path
        skill = parse_skill_file(
            md_path,
            SkillCategory.PUBLIC,
            relative_path=nested.relative_to(category_root),
        )

        assert skill is not None
        assert skill.relative_path.as_posix() == "subdir/nested-skill"
        assert skill.skill_path == "subdir/nested-skill"

    def test_name_with_special_chars_is_parsed_but_not_validated(self, tmp_path):
        """Parser extracts the name as-is; validation happens separately."""
        content = """---
name: "My Skill!"
description: Name with special chars
---
"""
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        # Parser is lenient — validation layer rejects bad names
        assert skill is not None
        assert skill.name == "My Skill!"

    def test_description_too_long_is_parsed(self, tmp_path):
        """Parser extracts long descriptions; validation layer truncates/rejects."""
        long_desc = "A" * 2000
        content = f"---\nname: long-desc\ndescription: {long_desc}\n---\n"
        md_path = _write_skill_md(tmp_path, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is not None
        assert len(skill.description) == 2000

    def test_container_path_helpers(self, tmp_path):
        content = """---
name: path-test
description: Testing container path helpers
---
"""
        skill_dir = tmp_path / "path-test"
        md_path = _write_skill_md(skill_dir, content)
        skill = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert skill is not None

        assert skill.get_container_path() == "/mnt/skills/public/path-test"
        assert (
            skill.get_container_file_path()
            == "/mnt/skills/public/path-test/SKILL.md"
        )
        # Custom base path
        assert (
            skill.get_container_path("/opt/skills")
            == "/opt/skills/public/path-test"
        )

    def test_custom_skill_category(self, tmp_path):
        content = """---
name: my-skill
description: A custom skill
---
"""
        skill_dir = tmp_path / "my-skill"
        md_path = _write_skill_md(skill_dir, content)
        skill = parse_skill_file(md_path, SkillCategory.CUSTOM)
        assert skill is not None
        assert skill.category == SkillCategory.CUSTOM
        assert skill.get_container_path() == "/mnt/skills/custom/my-skill"
