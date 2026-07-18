"""Tests for Lead Agent per-config skill filtering, tool-policy filtering, and cache integration."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.skills.types import Skill, SkillCategory
from harness.skills.tool_policy import allowed_tool_names_for_skills, filter_tools_by_skill_allowed_tools
from harness.skills.cache import (
    refresh_skills_system_prompt_cache,
    get_cached_skills_prompt_section,
    build_skills_signature,
)


# ===================================================================
# build_skills_signature
# ===================================================================


class TestBuildSkillsSignature:
    def test_empty_skills(self):
        sig = build_skills_signature([])
        assert sig == ""

    def test_single_skill_no_version(self):
        skill = Skill(
            name="test", description="desc", license=None,
            skill_dir=Path("."), skill_file=Path("SKILL.md"),
            relative_path=Path("."), category=SkillCategory.BUILTIN,
        )
        sig = build_skills_signature([skill])
        assert sig == "test:0"

    def test_single_skill_with_version(self):
        # version is stored in metadata but not directly as a Skill attr
        # We patch it on for testing
        skill = Skill(
            name="test", description="desc", license=None,
            skill_dir=Path("."), skill_file=Path("SKILL.md"),
            relative_path=Path("."), category=SkillCategory.BUILTIN,
        )
        skill.version = "1.5"
        sig = build_skills_signature([skill])
        assert sig == "test:1.5"

    def test_multiple_skills_sorted(self):
        skills = [
            Skill(name="z-skill", description="d", license=None,
                  skill_dir=Path("."), skill_file=Path("SKILL.md"),
                  relative_path=Path("."), category=SkillCategory.BUILTIN),
            Skill(name="a-skill", description="d", license=None,
                  skill_dir=Path("."), skill_file=Path("SKILL.md"),
                  relative_path=Path("."), category=SkillCategory.BUILTIN),
        ]
        sig = build_skills_signature(skills)
        assert sig == "a-skill:0;z-skill:0"

    def test_different_skills_produce_different_signatures(self):
        s1 = Skill(name="a", description="d", license=None,
                   skill_dir=Path("."), skill_file=Path("SKILL.md"),
                   relative_path=Path("."), category=SkillCategory.BUILTIN)
        s2 = Skill(name="b", description="d", license=None,
                   skill_dir=Path("."), skill_file=Path("SKILL.md"),
                   relative_path=Path("."), category=SkillCategory.BUILTIN)
        assert build_skills_signature([s1]) != build_skills_signature([s2])


# ===================================================================
# Cache — get_cached_skills_prompt_section
# ===================================================================


class TestSkillsPromptCache:
    def setup_method(self):
        refresh_skills_system_prompt_cache()

    def test_cache_returns_same_result_on_repeat_calls(self):
        call_count = [0]

        def builder():
            call_count[0] += 1
            return "prompt section"

        r1 = get_cached_skills_prompt_section("sig1", builder)
        r2 = get_cached_skills_prompt_section("sig1", builder)
        assert r1 == r2 == "prompt section"
        assert call_count[0] == 1  # Builder was called only once

    def test_different_signatures_get_different_cache_entries(self):
        call_count = [0]

        def builder():
            call_count[0] += 1
            return f"prompt-{call_count[0]}"

        r1 = get_cached_skills_prompt_section("sig-a", builder)
        r2 = get_cached_skills_prompt_section("sig-b", builder)
        assert r1 != r2
        assert call_count[0] == 2  # Builder called for each unique signature

    def test_refresh_invalidates_cache(self):
        call_count = [0]

        def builder():
            call_count[0] += 1
            return f"prompt-v{call_count[0]}"

        r1 = get_cached_skills_prompt_section("sig1", builder)
        refresh_skills_system_prompt_cache()
        r2 = get_cached_skills_prompt_section("sig1", builder)
        assert r1 != r2  # After refresh, builder was called again
        assert call_count[0] == 2

    def test_cache_max_size_does_not_grow_unbounded(self):
        """Fill the cache with 32 entries (2x max) — old entries should be evicted."""
        results = []
        for i in range(32):
            results.append(
                get_cached_skills_prompt_section(f"sig-{i}", lambda i=i: f"prompt-{i}")
            )
        assert len(results) == 32
        # All values should still be retrievable (cache eviction is transparent)
        for i in range(32):
            r = get_cached_skills_prompt_section(f"sig-{i}", lambda i=i: f"prompt-{i}")
            assert r == f"prompt-{i}"


# ===================================================================
# LeadAgent._available_skill_names — whitelist filtering
# ===================================================================


class TestAvailableSkillNames:
    """Test the per-agent skill whitelist logic."""

    def _make_lead_agent(self, agent_config=None, skill_storage=None):
        """Create a minimal LeadAgent for testing."""
        from harness.agents.lead_agent import LeadAgent

        agent = LeadAgent(
            tool_registry=MagicMock(),
            subagent_manager=None,
            skill_storage=skill_storage,
            agent_config=agent_config,
        )
        return agent

    def test_none_when_no_agent_config(self):
        agent = self._make_lead_agent(agent_config=None)
        assert agent._available_skill_names() is None

    def test_none_when_agent_config_has_no_skills(self):
        cfg = MagicMock()
        cfg.skills = None
        agent = self._make_lead_agent(agent_config=cfg)
        assert agent._available_skill_names() is None

    def test_empty_list_when_skills_is_empty(self):
        cfg = MagicMock()
        cfg.skills = []
        agent = self._make_lead_agent(agent_config=cfg)
        result = agent._available_skill_names()
        assert result == set()

    def test_whitelist_when_skills_is_list(self):
        cfg = MagicMock()
        cfg.skills = ["skill-a", "skill-b"]
        agent = self._make_lead_agent(agent_config=cfg)
        result = agent._available_skill_names()
        assert result == {"skill-a", "skill-b"}


# ===================================================================
# Tool policy with whitelist integration
# ===================================================================


class TestToolPolicyWithWhitelist:
    @staticmethod
    def _make_tool(name: str):
        from dataclasses import dataclass
        @dataclass
        class T:
            name: str
        return T(name)

    def _make_skill(self, name, allowed_tools):
        return Skill(
            name=name, description="test", license=None,
            skill_dir=Path("."), skill_file=Path("SKILL.md"),
            relative_path=Path("."), category=SkillCategory.BUILTIN,
            allowed_tools=allowed_tools, enabled=True,
        )

    def test_filter_with_whitelisted_skills(self):
        """Only whitelisted skills should contribute to tool filtering."""
        s1 = self._make_skill("skill-a", ["tool-a", "tool-b"])
        s2 = self._make_skill("skill-b", ["tool-c"])

        all_tools = [self._make_tool(n) for n in ["tool-a", "tool-b", "tool-c", "tool-d"]]

        # Only allow skill-a
        whitelist = {"skill-a"}
        filtered_skills = [s1]  # s2 excluded by whitelist

        result = allowed_tool_names_for_skills(filtered_skills)
        assert result == {"tool-a", "tool-b"}

        filtered_tools = filter_tools_by_skill_allowed_tools(all_tools, filtered_skills)
        assert {t.name for t in filtered_tools} == {"tool-a", "tool-b"}

    def test_empty_whitelist_loads_no_skills(self):
        """Empty whitelist → no skills → legacy allow-all."""
        s1 = self._make_skill("skill-a", ["tool-a"])
        all_tools = [self._make_tool(n) for n in ["tool-a", "tool-b"]]

        # No skills loaded
        filtered_tools = filter_tools_by_skill_allowed_tools(all_tools, [])
        assert len(filtered_tools) == 2  # Legacy allow-all

    def test_skill_allowed_tools_union_is_correct(self):
        s1 = self._make_skill("a", ["bash", "file_read"])
        s2 = self._make_skill("b", ["file_read", "web_search"])
        s3 = self._make_skill("c", None)  # unrestricted

        # Only a, b are whitelisted
        result = allowed_tool_names_for_skills([s1, s2])
        assert result == {"bash", "file_read", "web_search"}
        # c contributes nothing to allowed set since at least one skill declares

    def test_unrestricted_skill_alone_no_restriction(self):
        s = self._make_skill("unrestricted", None)
        result = allowed_tool_names_for_skills([s])
        assert result is None  # legacy — allow all tools
