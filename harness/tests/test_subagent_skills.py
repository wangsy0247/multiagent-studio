"""Tests for subagent skill loading, tool filtering, and parent-child skill permission merging."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.skills.types import Skill, SkillCategory
from harness.agents.subagent_executor import SubagentExecutor


# ===================================================================
# _merge_skill_allowlists
# ===================================================================


class TestMergeSkillAllowlists:
    def test_both_none_returns_none(self):
        result = SubagentExecutor._merge_skill_allowlists(None, None)
        assert result is None

    def test_parent_none_child_list(self):
        result = SubagentExecutor._merge_skill_allowlists(None, ["a", "b"])
        assert result == ["a", "b"]

    def test_parent_list_child_none(self):
        result = SubagentExecutor._merge_skill_allowlists(["a", "b"], None)
        assert result == ["a", "b"]

    def test_intersection(self):
        result = SubagentExecutor._merge_skill_allowlists(["a", "b", "c"], ["b", "c", "d"])
        assert sorted(result) == ["b", "c"]

    def test_no_overlap_returns_empty(self):
        result = SubagentExecutor._merge_skill_allowlists(["a", "b"], ["c", "d"])
        assert result == []

    def test_parent_empty_returns_empty(self):
        """Parent with empty list always yields empty for child."""
        result = SubagentExecutor._merge_skill_allowlists([], ["a", "b"])
        assert result == []

    def test_parent_empty_child_none(self):
        result = SubagentExecutor._merge_skill_allowlists([], None)
        assert result == []

    def test_parent_empty_child_empty(self):
        result = SubagentExecutor._merge_skill_allowlists([], [])
        assert result == []

    def test_identical_lists(self):
        result = SubagentExecutor._merge_skill_allowlists(["a", "b"], ["a", "b"])
        assert sorted(result) == ["a", "b"]

    def test_single_each(self):
        result = SubagentExecutor._merge_skill_allowlists(["x"], ["x"])
        assert result == ["x"]

    def test_single_mismatch(self):
        result = SubagentExecutor._merge_skill_allowlists(["x"], ["y"])
        assert result == []

    def test_preserves_order_from_child(self):
        """Result should preserve child's order."""
        result = SubagentExecutor._merge_skill_allowlists(["c", "b", "a"], ["a", "b", "c", "d"])
        assert result == ["a", "b", "c"]


# ===================================================================
# SubagentExecutor._load_skills
# ===================================================================


def _make_skill(name, category=SkillCategory.BUILTIN, enabled=True, allowed=None):
    return Skill(
        name=name, description=f"Description for {name}", license=None,
        skill_dir=Path(f"/fake/skills/{category}/{name}"),
        skill_file=Path(f"/fake/skills/{category}/{name}/SKILL.md"),
        relative_path=Path(name), category=category,
        allowed_tools=allowed, enabled=enabled,
    )


class TestSubagentLoadSkills:
    def _make_executor(self, config_skills=None, parent_skills=None, storage_skills=None):
        """Create a SubagentExecutor with mocked skill storage."""
        from harness.models import SubAgentConfig

        config = SubAgentConfig(
            name="test-sub",
            display_name="Test SubAgent",
            system_prompt="You are a tester.",
            skills=config_skills,
        )

        storage = MagicMock()
        storage.load_skills.return_value = storage_skills or []

        executor = SubagentExecutor(
            config=config,
            llm=MagicMock(),
            tools=[],
            skill_storage=storage,
            parent_skills=parent_skills,
        )
        return executor

    def test_no_skill_storage_returns_empty(self):
        executor = SubagentExecutor(
            config=MagicMock(skills=None),
            llm=MagicMock(),
            tools=[],
            skill_storage=None,
        )
        assert executor._load_skills() == []

    def test_all_enabled_when_no_constraints(self):
        skills = [_make_skill("a"), _make_skill("b")]
        executor = self._make_executor(
            config_skills=None, parent_skills=None, storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert len(loaded) == 2

    def test_child_whitelist_filters(self):
        skills = [_make_skill("a"), _make_skill("b"), _make_skill("c")]
        executor = self._make_executor(
            config_skills=["a", "c"], parent_skills=None, storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert {s.name for s in loaded} == {"a", "c"}

    def test_parent_whitelist_filters(self):
        skills = [_make_skill("a"), _make_skill("b"), _make_skill("c")]
        executor = self._make_executor(
            config_skills=None, parent_skills=["a"], storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert {s.name for s in loaded} == {"a"}

    def test_intersection_filters(self):
        """Child wants a, b; parent allows a, c → child gets a."""
        skills = [_make_skill("a"), _make_skill("b"), _make_skill("c")]
        executor = self._make_executor(
            config_skills=["a", "b"], parent_skills=["a", "c"], storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert {s.name for s in loaded} == {"a"}

    def test_child_empty_list_no_skills(self):
        """Child declares [] → explicitly no skills."""
        skills = [_make_skill("a"), _make_skill("b")]
        executor = self._make_executor(
            config_skills=[], parent_skills=None, storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert loaded == []

    def test_parent_empty_list_no_skills(self):
        """Parent declares [] → child gets none."""
        skills = [_make_skill("a"), _make_skill("b")]
        executor = self._make_executor(
            config_skills=None, parent_skills=[], storage_skills=skills,
        )
        loaded = executor._load_skills()
        assert loaded == []

    def test_storage_exception_returns_empty(self):
        """Storage failure → empty skills, no crash."""
        storage = MagicMock()
        storage.load_skills.side_effect = RuntimeError("Disk error")

        from harness.models import SubAgentConfig

        config = SubAgentConfig(
            name="test-sub",
            display_name="Test",
            system_prompt="You are a tester.",
        )
        executor = SubagentExecutor(
            config=config, llm=MagicMock(), tools=[],
            skill_storage=storage,
        )
        loaded = executor._load_skills()
        assert loaded == []


# ===================================================================
# SubagentExecutor._build_skill_messages
# ===================================================================


class TestBuildSkillMessages:
    def _make_executor(self):
        from harness.models import SubAgentConfig

        config = SubAgentConfig(
            name="test-sub",
            display_name="Test",
            system_prompt="You are a tester.",
        )
        return SubagentExecutor(
            config=config, llm=MagicMock(), tools=[],
        )

    def test_empty_skills_no_messages(self, tmp_path):
        executor = self._make_executor()
        msgs = executor._build_skill_messages([])
        assert msgs == []

    def test_skill_content_injected_as_system_message(self, tmp_path):
        """Skill content is injected as SystemMessage with skill XML wrapper."""
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test Skill\n\nDo the thing.")

        skill = Skill(
            name="test-skill", description="desc", license=None,
            skill_dir=tmp_path, skill_file=skill_file,
            relative_path=Path("."), category=SkillCategory.BUILTIN,
        )
        executor = self._make_executor()
        msgs = executor._build_skill_messages([skill])
        assert len(msgs) == 1
        assert 'skill name="test-skill"' in msgs[0].content
        assert "Do the thing." in msgs[0].content

    def test_failed_read_skipped(self):
        """When skill file read fails, that skill is skipped."""
        skill = Skill(
            name="bad-skill", description="desc", license=None,
            skill_dir=Path("/nonexistent"), skill_file=Path("/nonexistent/SKILL.md"),
            relative_path=Path("."), category=SkillCategory.BUILTIN,
        )
        executor = self._make_executor()
        msgs = executor._build_skill_messages([skill])
        assert msgs == []

    def test_multiple_skills_produce_multiple_messages(self, tmp_path):
        f1 = tmp_path / "s1" / "SKILL.md"
        f1.parent.mkdir()
        f1.write_text("# Skill 1")
        f2 = tmp_path / "s2" / "SKILL.md"
        f2.parent.mkdir()
        f2.write_text("# Skill 2")

        skills = [
            Skill(name="s1", description="d1", license=None,
                  skill_dir=f1.parent, skill_file=f1,
                  relative_path=Path("."), category=SkillCategory.BUILTIN),
            Skill(name="s2", description="d2", license=None,
                  skill_dir=f2.parent, skill_file=f2,
                  relative_path=Path("."), category=SkillCategory.BUILTIN),
        ]
        executor = self._make_executor()
        msgs = executor._build_skill_messages(skills)
        assert len(msgs) == 2
        assert "Skill 1" in msgs[0].content
        assert "Skill 2" in msgs[1].content


# ===================================================================
# Integration: full _build_initial_state with skills
# ===================================================================


class TestBuildInitialStateWithSkills:
    def test_skill_messages_included_in_state(self, tmp_path):
        """Skill SystemMessages are injected before the agent system prompt."""
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test Skill\n\nWorkflow instructions.")

        skill = Skill(
            name="test-skill", description="desc", license=None,
            skill_dir=tmp_path, skill_file=skill_file,
            relative_path=Path("."), category=SkillCategory.BUILTIN,
        )

        from harness.models import SubAgentConfig

        config = SubAgentConfig(
            name="test-sub",
            display_name="Test",
            system_prompt="You are a subagent.",
        )
        executor = SubagentExecutor(
            config=config, llm=MagicMock(), tools=[],
        )
        executor._skills = [skill]

        state = executor._build_initial_state("Do task X")
        msgs = state["messages"]
        # Skill message(s) + system prompt + human message
        assert len(msgs) == 3
        assert "Workflow instructions" in msgs[0].content
        assert msgs[1].content == "You are a subagent."
        assert msgs[2].content == "Do task X"

    def test_no_skills_still_works(self):
        """Without skills, initial state is just system_prompt + task."""
        from harness.models import SubAgentConfig

        config = SubAgentConfig(
            name="test-sub",
            display_name="Test",
            system_prompt="You are a subagent.",
        )
        executor = SubagentExecutor(
            config=config, llm=MagicMock(), tools=[],
        )
        executor._skills = []

        state = executor._build_initial_state("Do task X")
        msgs = state["messages"]
        assert len(msgs) == 2
        assert msgs[0].content == "You are a subagent."
        assert msgs[1].content == "Do task X"
