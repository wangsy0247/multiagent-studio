"""Tests for agent modules."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from harness.models import SubAgentConfig, SubAgentResult, initial_state
from harness.agents.presets import PRESET_SUBAGENTS, build_subagent_config
from harness.agents.subagent_manager import SubagentManager
from harness.tools.registry import ToolRegistry


class TestPresets:
    def test_all_presets_defined(self):
        assert "researcher" in PRESET_SUBAGENTS
        assert "coder" in PRESET_SUBAGENTS
        assert "analyst" in PRESET_SUBAGENTS
        assert "writer" in PRESET_SUBAGENTS
        assert "reviewer" in PRESET_SUBAGENTS

    def test_preset_has_required_fields(self):
        for name, preset in PRESET_SUBAGENTS.items():
            assert "display_name" in preset, f"{name}: missing display_name"
            assert "description" in preset, f"{name}: missing description"
            assert "system_prompt" in preset, f"{name}: missing system_prompt"
            assert "tools" in preset, f"{name}: missing tools"

    def test_build_subagent_config(self):
        config = build_subagent_config("my-coder", "coder")
        assert isinstance(config, SubAgentConfig)
        assert config.name == "my-coder"
        assert config.model == "inherit"  # DeerFlow-style default

    def test_build_with_custom_prompt(self):
        config = build_subagent_config("custom", "coder", custom_system_prompt="Custom prompt")
        assert config.system_prompt == "Custom prompt"

    def test_build_unknown_type_falls_back(self):
        config = build_subagent_config("unknown", "nonexistent_type")
        assert isinstance(config, SubAgentConfig)  # Falls back to something


class TestSubagentManager:
    @pytest.fixture
    def registry(self):
        # Tools are now loaded from config.yaml; the registry can be empty for
        # these manager lifecycle tests.
        return ToolRegistry()

    @pytest.fixture
    def llm_factory(self):
        from langchain_openai import ChatOpenAI
        # Return a mock LLM since we don't actually call it
        mock = MagicMock()
        mock.return_value = mock
        return lambda model: mock

    @pytest.fixture
    def manager(self, llm_factory, registry):
        return SubagentManager(
            llm_factory=llm_factory,
            tool_registry=registry,
            max_concurrent=3,
        )

    @pytest.mark.asyncio
    async def test_create_subagent(self, manager):
        config = build_subagent_config("test-agent", "coder")
        agent = await manager.create(config)
        assert agent is not None
        assert manager.get("test-agent") is not None

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, manager):
        config = build_subagent_config("dup-agent", "coder")
        await manager.create(config)
        with pytest.raises(ValueError, match="已存在"):
            await manager.create(config)

    @pytest.mark.asyncio
    async def test_list_agents(self, manager):
        c1 = build_subagent_config("agent-a", "coder")
        c2 = build_subagent_config("agent-b", "writer")
        await manager.create(c1)
        await manager.create(c2)
        agents = manager.list()
        assert len(agents) == 2

    @pytest.mark.asyncio
    async def test_delete_agent(self, manager):
        config = build_subagent_config("temp-agent", "reviewer")
        await manager.create(config)
        await manager.delete("temp-agent")
        assert manager.get("temp-agent") is None

    def test_get_nonexistent(self, manager):
        assert manager.get("nonexistent") is None

    def test_max_concurrent_clamped(self, llm_factory, registry):
        m1 = SubagentManager(llm_factory, registry, max_concurrent=10)
        assert m1._max_concurrent == 4
        m2 = SubagentManager(llm_factory, registry, max_concurrent=1)
        assert m2._max_concurrent == 2
