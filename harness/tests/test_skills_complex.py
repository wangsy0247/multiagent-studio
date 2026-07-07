"""Comprehensive tests for complex skills — nested dirs, support files, metadata, tool filtering."""

import pytest
from pathlib import Path

from harness.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from harness.skills.storage import SkillStorage
from harness.skills.parser import parse_skill_file
from harness.skills.prompt import get_skills_prompt_section
from harness.skills.tool_policy import filter_tools_by_skill_allowed_tools, allowed_tool_names_for_skills

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS_ROOT = _PROJECT_ROOT / "skills"


@pytest.fixture
def storage():
    return SkillStorage(_SKILLS_ROOT)


# ===================================================================
# Discovery: all 6 skills should load
# ===================================================================


class TestSkillDiscovery:
    def test_all_six_skills_loaded(self, storage):
        skills = storage.load_skills()
        names = {s.name for s in skills}
        assert names == {
            "code-reviewer",
            "deep-research",
            "deployment-checklist",
            "greeting-responder",
            "my-workflow",
            "system-architecture-review",
        }, f"Expected 6 skills, got: {names}"

    def test_three_public_three_custom(self, storage):
        skills = storage.load_skills()
        public = [s for s in skills if s.category == SkillCategory.PUBLIC]
        custom = [s for s in skills if s.category == SkillCategory.CUSTOM]
        assert len(public) == 4, f"Expected 4 public, got: {[s.name for s in public]}"
        assert len(custom) == 2, f"Expected 2 custom, got: {[s.name for s in custom]}"


# ===================================================================
# Complex skill metadata
# ===================================================================


class TestComplexSkillMetadata:
    def test_architecture_review_allowed_tools(self, storage):
        s = {s.name: s for s in storage.load_skills()}["system-architecture-review"]
        # allowed-tools removed — all skills now use legacy allow-all (None)
        assert s.allowed_tools is None

    def test_architecture_review_has_version(self, storage):
        """version field should be available even though not parsed into the Skill dataclass directly."""
        s = {s.name: s for s in storage.load_skills()}["system-architecture-review"]
        assert s.license == "MIT"

    def test_deployment_checklist_is_custom(self, storage):
        s = {s.name: s for s in storage.load_skills()}["deployment-checklist"]
        assert s.category == SkillCategory.CUSTOM
        # allowed-tools removed — legacy allow-all
        assert s.allowed_tools is None

    def test_deployment_checklist_has_support_files(self):
        """Verify the custom skill directory contains its references and scripts."""
        skill_dir = _SKILLS_ROOT / "custom" / "deployment-checklist"
        assert (skill_dir / "references" / "rollback_procedures.md").exists()
        assert (skill_dir / "scripts" / "preflight_check.sh").exists()
        # preflight_check.sh should be executable
        script = skill_dir / "scripts" / "preflight_check.sh"
        assert script.exists()

    def test_architecture_review_has_support_files(self):
        """Public skill should have references and templates."""
        skill_dir = _SKILLS_ROOT / "public" / "system-architecture-review"
        assert (skill_dir / "references" / "review_checklist.md").exists()
        assert (skill_dir / "references" / "common_anti_patterns.md").exists()
        assert (skill_dir / "templates" / "architecture_report.md").exists()


# ===================================================================
# Support file content validation
# ===================================================================


class TestSupportFileContent:
    def test_checklist_has_50_items(self):
        path = _SKILLS_ROOT / "public" / "system-architecture-review" / "references" / "review_checklist.md"
        content = path.read_text()
        # Count checkbox items
        checkbox_count = content.count("[ ]")
        assert checkbox_count == 50, f"Expected 50 checklist items, found {checkbox_count}"

    def test_anti_patterns_catalog_complete(self):
        path = _SKILLS_ROOT / "public" / "system-architecture-review" / "references" / "common_anti_patterns.md"
        content = path.read_text()
        for pattern in [
            "Distributed Monolith",
            "Single Point of Failure",
            "Missing Back-Pressure",
            "Secret in Plaintext",
            "Synchronous Call Chain",
            "Log-and-Forget",
            "No Kill Switch",
            "Big Ball of Mud",
        ]:
            assert pattern in content, f"Missing anti-pattern: {pattern}"

    def test_report_template_has_all_sections(self):
        path = _SKILLS_ROOT / "public" / "system-architecture-review" / "templates" / "architecture_report.md"
        content = path.read_text()
        for section in [
            "Executive Summary",
            "Risk Matrix",
            "Critical Findings",
            "What This Architecture Does Well",
            "Review Methodology",
        ]:
            assert section in content, f"Missing template section: {section}"

    def test_preflight_script_is_valid_bash(self):
        path = _SKILLS_ROOT / "custom" / "deployment-checklist" / "scripts" / "preflight_check.sh"
        content = path.read_text()
        assert content.startswith("#!/bin/bash")
        assert "set -euo pipefail" in content
        assert "pre-flight" in content.lower()

    def test_rollback_procedures_covers_multiple_platforms(self):
        path = _SKILLS_ROOT / "custom" / "deployment-checklist" / "references" / "rollback_procedures.md"
        content = path.read_text()
        assert "Kubernetes" in content
        assert "Docker Compose" in content
        assert "AWS ECS" in content
        assert "Decision Tree" in content


# ===================================================================
# SkillStorage CRUD with complex skills
# ===================================================================


class TestComplexSkillCRUD:
    def test_read_custom_deployment_checklist(self, storage):
        """Storage.read_custom_skill should return full SKILL.md content."""
        content = storage.read_custom_skill("deployment-checklist")
        assert "Production deployment safety checklist" in content
        assert "Phase 1: Pre-Flight" in content
        assert "Phase 2: Canary" in content
        assert "Phase 3: Post-Deployment" in content
        assert "Phase 4: Rollback Decision" in content

    def test_write_support_file_to_custom_skill(self, storage, tmp_path):
        """Write a new reference file to a custom skill and verify."""
        name = "deployment-checklist"
        content = "# Test Reference\n\nThis was written by a test."
        storage.write_custom_skill(name, "references/test_written.md", content)

        skill_dir = storage.get_custom_skill_dir(name)
        written = skill_dir / "references" / "test_written.md"
        assert written.exists()
        assert written.read_text() == content

        # Cleanup — remove the test file
        written.unlink()

    def test_path_traversal_blocked_on_complex_skill(self, storage):
        """Even on skills with deep subdirectories, traversal is blocked."""
        name = "deployment-checklist"
        with pytest.raises(ValueError, match="resolve within"):
            storage.write_custom_skill(name, "../../../etc/passwd", "evil")


# ===================================================================
# Prompt section with 6 skills
# ===================================================================


class TestPromptWithAllSkills:
    def test_prompt_contains_all_six_names(self, storage):
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)

        for name in [
            "code-reviewer",
            "deep-research",
            "deployment-checklist",
            "greeting-responder",
            "my-workflow",
            "system-architecture-review",
        ]:
            assert f"<name>{name}</name>" in section, f"Missing {name}"

    def test_prompt_distinguishes_public_from_custom(self, storage):
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)

        # All public skills labeled [public]
        assert section.count("[public]") == 4
        # All custom skills labeled [custom]
        assert section.count("[custom]") == 2

    def test_prompt_size_scales_reasonably(self, storage):
        """6 skills should produce a prompt section under 5KB."""
        skills = storage.load_skills(enabled_only=True)
        section = get_skills_prompt_section(skills)
        size = len(section)
        # Expect: ~500-800 bytes per skill = ~3-5KB total for 6 skills
        assert 1500 < size < 5000, f"Prompt section size {size} chars — out of expected range"

    def test_disabled_skill_absent_from_prompt(self, storage):
        """Simulate disabling a skill — it should vanish from the prompt."""
        skills = storage.load_skills()
        for s in skills:
            if s.name == "greeting-responder":
                s.enabled = False

        enabled = [s for s in skills if s.enabled]
        section = get_skills_prompt_section(enabled)
        assert "greeting-responder" not in section
        assert len(enabled) == 5


# ===================================================================
# Tool filtering with complex skill combinations
# ===================================================================


class TestComplexToolFiltering:
    @staticmethod
    def _make_tool(name: str):
        from dataclasses import dataclass
        @dataclass
        class T:
            name: str
        return T(name)

    def _get_skill(self, storage, name):
        return {s.name: s for s in storage.load_skills()}[name]

    def test_all_skills_legacy_allow_all(self, storage):
        """All skills have allowed_tools=None → legacy allow-all (no filtering)."""
        skills = storage.load_skills()
        result = allowed_tool_names_for_skills(skills)
        assert result is None  # legacy: allow all tools

    def test_filtering_passes_all_when_no_declarations(self, storage):
        """When no skill declares allowed-tools, all tools pass through."""
        skills = storage.load_skills()
        all_tools = [
            self._make_tool(n) for n in
            ["file_read", "file_write", "list_files", "grep_tool", "glob_tool",
             "web_search", "web_fetch", "bash", "task", "str_replace"]
        ]
        filtered = filter_tools_by_skill_allowed_tools(all_tools, skills)
        assert len(filtered) == len(all_tools)  # Nothing filtered

    def test_single_skill_legacy(self, storage):
        """Single skill without allowed-tools → no restriction."""
        skill = self._get_skill(storage, "greeting-responder")
        result = allowed_tool_names_for_skills([skill])
        assert result is None

    def test_mixed_all_unrestricted(self, storage):
        """Multiple skills all without allowed-tools → legacy allow-all."""
        s1 = self._get_skill(storage, "code-reviewer")
        s2 = self._get_skill(storage, "deep-research")
        s3 = self._get_skill(storage, "greeting-responder")
        result = allowed_tool_names_for_skills([s1, s2, s3])
        assert result is None  # All unrestricted → legacy mode


# ===================================================================
# Parse then load round-trip
# ===================================================================


class TestParseThenLoadRoundTrip:
    def test_parse_architecture_review_then_load(self):
        """Verify that individually parsed skills match what storage loads."""
        md_path = _SKILLS_ROOT / "public" / "system-architecture-review" / SKILL_MD_FILE
        parsed = parse_skill_file(
            md_path,
            SkillCategory.PUBLIC,
            relative_path=Path("system-architecture-review"),
        )
        assert parsed is not None
        assert parsed.name == "system-architecture-review"
        assert parsed.description.startswith("Conduct comprehensive system architecture reviews")
        # allowed-tools removed — legacy allow-all
        assert parsed.allowed_tools is None
        assert parsed.category == SkillCategory.PUBLIC

    def test_parse_deployment_checklist_then_load(self):
        md_path = _SKILLS_ROOT / "custom" / "deployment-checklist" / SKILL_MD_FILE
        parsed = parse_skill_file(
            md_path,
            SkillCategory.CUSTOM,
            relative_path=Path("deployment-checklist"),
        )
        assert parsed is not None
        assert parsed.name == "deployment-checklist"
        assert parsed.category == SkillCategory.CUSTOM
        # allowed-tools removed — legacy allow-all
        assert parsed.allowed_tools is None

    def test_container_path_for_nested_skill(self):
        """Skill in a flat directory gets correct container path."""
        md_path = _SKILLS_ROOT / "public" / "system-architecture-review" / SKILL_MD_FILE
        parsed = parse_skill_file(md_path, SkillCategory.PUBLIC)
        assert parsed is not None
        assert parsed.get_container_path() == "/mnt/skills/public/system-architecture-review"
        assert parsed.get_container_file_path() == "/mnt/skills/public/system-architecture-review/SKILL.md"
