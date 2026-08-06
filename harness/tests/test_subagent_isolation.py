"""P0 修复回归: SubAgent 注册表按属主隔离 + 流队列按 thread 隔离."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from harness.agents import subagent_manager as sm
from harness.agents.subagent_executor import (
    _stream_key,
    get_subagent_stream,
    list_active_subagent_names,
    remove_subagent_stream,
)
from harness.agents.subagent_manager import SubagentManager, set_current_owner
from harness.models import SubAgentConfig


@pytest.fixture(autouse=True)
def _reset_owner():
    set_current_owner("default")
    yield
    set_current_owner("default")


def _make_manager() -> SubagentManager:
    registry = MagicMock()
    registry.get_core_tools.return_value = []
    return SubagentManager(
        llm_factory=lambda model=None: MagicMock(name=f"llm-{model}"),
        tool_registry=registry,
    )


def _cfg(name: str) -> SubAgentConfig:
    return SubAgentConfig(name=name, display_name=name, system_prompt="s")


class TestOwnerIsolation:
    @pytest.mark.asyncio
    async def test_same_name_different_owners_do_not_collide(self):
        mgr = _make_manager()

        set_current_owner("user-a")
        await mgr.create(_cfg("coder"))
        assert mgr.get("coder") is not None

        # user-b 看不到 user-a 的 subagent (缓存的 LLM 凭证不跨用户复用)
        set_current_owner("user-b")
        assert mgr.get("coder") is None
        assert mgr.list() == []

        # user-b 可创建同名 subagent — 独立条目
        await mgr.create(_cfg("coder"))
        assert mgr.get("coder") is not None
        assert len(mgr._agents) == 2

        # user-a 的仍然独立存在
        set_current_owner("user-a")
        assert mgr.get("coder") is not None
        assert len(mgr.list()) == 1

    @pytest.mark.asyncio
    async def test_delete_only_affects_current_owner(self):
        mgr = _make_manager()
        set_current_owner("user-a")
        await mgr.create(_cfg("coder"))
        set_current_owner("user-b")
        await mgr.create(_cfg("coder"))

        await mgr.delete("coder")  # 删 user-b 的
        assert mgr.get("coder") is None
        set_current_owner("user-a")
        assert mgr.get("coder") is not None

    def test_last_results_scoped_by_owner(self):
        mgr = _make_manager()
        result = MagicMock()

        set_current_owner("user-a")
        mgr._last_results[sm._owner_key("coder")] = result

        # user-b pop 不到 user-a 的结果 (跨用户结果泄漏)
        set_current_owner("user-b")
        assert mgr.pop_last_result("coder") is None

        set_current_owner("user-a")
        assert mgr.pop_last_result("coder") is result


class TestStreamIsolation:
    def test_stream_key_includes_thread(self):
        assert _stream_key("t1", "coder") == "t1:coder"
        assert _stream_key("", "coder") == "default:coder"

    def test_list_filters_by_thread(self):
        get_subagent_stream(_stream_key("t1", "coder"))
        get_subagent_stream(_stream_key("t2", "coder"))
        try:
            assert list_active_subagent_names("t1") == ["t1:coder"]
            assert list_active_subagent_names("t2") == ["t2:coder"]
            assert len(list_active_subagent_names()) == 2
        finally:
            remove_subagent_stream(_stream_key("t1", "coder"))
            remove_subagent_stream(_stream_key("t2", "coder"))

    def test_remove_does_not_touch_other_thread(self):
        k1 = _stream_key("t1", "coder")
        k2 = _stream_key("t2", "coder")
        s1 = get_subagent_stream(k1)
        s2 = get_subagent_stream(k2)
        remove_subagent_stream(k1)
        try:
            # t2 的队列仍在且是同一个对象
            assert get_subagent_stream(k2) is s2
            assert list_active_subagent_names() == [k2]
        finally:
            remove_subagent_stream(k2)
