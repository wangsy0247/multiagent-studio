"""P2 修复回归测试.

覆盖:
  P2-10: 消息实时唤醒 — agent 的 _wake_event 与总线通知事件是同一个对象
  P2-9:  read_inbox 工具对协议消息走结构化路由 (登记 _pending_requests)
  P2-13: idle 工具已移除
  P2-11: 死代码清理 (TeamExecutionMode / MAX_TEAM_ROUNDS / project_lead_agent)
  P2-12: TeamContext.members 保鲜; prompt 反映最新成员名单
  P2-14: cancel_stale_tasks 只清团队遗留任务, 保留用户任务; TeamTask.origin 默认值

运行:  cd multiagent-studio && python -m pytest harness/tests/test_team_p2_fixes.py -v
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

logging.getLogger("harness.team").setLevel(logging.WARNING)


# ── 辅助: 自定义目录 (与 test_team_flow.py 相同) ──

def _patch_task_store():
    from harness.team.task_store import TeamTaskStore

    @classmethod
    def _create_with_dir(cls, base_dir: Path, project_id: str):
        store = cls.__new__(cls)
        store._project_id = project_id
        store._user_id = "default"
        store._tasks_dir = base_dir
        store._tasks_dir.mkdir(parents=True, exist_ok=True)
        store._file = base_dir / f"{project_id}.json"
        store._cache = None
        store._cache_mtime = 0.0
        return store

    TeamTaskStore._create_with_dir = _create_with_dir


def _patch_message_bus():
    from harness.team.message_bus import TeamMessageBus

    @classmethod
    def _create_with_dir(cls, base_dir: Path, project_id: str):
        bus = cls.__new__(cls)
        bus._project_id = project_id
        bus._user_id = "default"
        bus._inbox_dir = base_dir
        bus._inbox_dir.mkdir(parents=True, exist_ok=True)
        bus._known_agents: set[str] = set()
        bus._events: dict[str, asyncio.Event] = {}
        return bus

    TeamMessageBus._create_with_dir = _create_with_dir


_patch_task_store()
_patch_message_bus()


@pytest.fixture
def deps(tmp_path):
    from harness.team.task_store import TeamTaskStore
    from harness.team.message_bus import TeamMessageBus
    from harness.team.context import TeamContext
    from harness.team.models import TeamMemberRuntime, TeammateStatus
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    store = TeamTaskStore._create_with_dir(tmp_path / "tasks", "test_proj")
    bus = TeamMessageBus._create_with_dir(tmp_path / "msgs", "test_proj")
    ctx = TeamContext(
        project_id="test_proj",
        project_name="Test Project",
        project_description="A test project",
        thread_id="thread-1",
        user_id="default",
        members=[
            TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.IDLE),
            TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.IDLE),
        ],
    )
    llm = FakeListChatModel(responses=["OK"])
    return {"store": store, "bus": bus, "ctx": ctx, "llm": llm}


def _make_agent(name: str, deps: dict, role: str = "member"):
    from harness.team.teammate_agent import TeammateAgent

    return TeammateAgent(
        agent_name=name,
        llm=deps["llm"],
        tools=[],
        team_context=deps["ctx"],
        message_bus=deps["bus"],
        task_store=deps["store"],
        role=role,
        thread_id="thread-1",
        project_id="test_proj",
    )


# ============================================================================
# P2-10: 消息实时唤醒
# ============================================================================

class TestRealtimeWake:
    """agent 的唤醒事件与总线通知事件统一: send 即唤醒, 不等轮询."""

    def test_send_sets_wake_event_immediately(self, deps):
        from harness.team.models import TeamMessage, TeamMessageType

        async def _test():
            agent = _make_agent("alice", deps)
            assert agent._wake_event is deps["bus"].get_event("alice")

            agent._wake_event.clear()
            await deps["bus"].send(TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.TEXT, content="新任务线索",
            ))
            # 不等 5s 轮询 — send 后事件立即置位
            assert agent._wake_event.is_set()

        asyncio.run(_test())


# ============================================================================
# P2-9: read_inbox 协议消息统一路由
# ============================================================================

class TestReadInboxProtocolRouting:
    """协议消息经 read_inbox 工具读取时, 同样登记协议状态机."""

    def test_shutdown_request_registered_via_tool(self, deps):
        from harness.team.models import TeamMessage, TeamMessageType
        from harness.team.tools import (
            create_team_tools, set_current_agent, set_current_agent_instance,
        )

        async def _test():
            bus = deps["bus"]
            agent = _make_agent("alice", deps)

            await bus.send(TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.SHUTDOWN_REQUEST,
                content="请关机", request_id="req_test_1",
            ))

            tools = create_team_tools(message_bus=bus, role="member")
            tool = next(t for t in tools if t.name == "read_inbox")

            set_current_agent("alice")
            set_current_agent_instance(agent)
            try:
                result = await tool.ainvoke({})
            finally:
                set_current_agent_instance(None)

            # 协议状态机已登记 (与 InboxDrainMiddleware 路径一致)
            assert "req_test_1" in agent._pending_requests
            assert agent._pending_requests["req_test_1"]["type"] == "shutdown"
            assert "协议" in result

        asyncio.run(_test())


# ============================================================================
# P2-13 & P2-11: 死代码清理
# ============================================================================

class TestDeadCodeCleanup:
    def test_idle_tool_removed(self):
        from harness.team.tools import create_team_tools, MEMBER_TOOLS

        assert "idle" not in MEMBER_TOOLS
        member_tools = create_team_tools(role="member")
        assert "idle" not in {t.name for t in member_tools}
        assert len(member_tools) == 8  # SHARED 5 + MEMBER 3

    def test_dead_symbols_removed(self):
        from harness.team import models, orchestrator

        assert not hasattr(models, "TeamExecutionMode")
        assert not hasattr(orchestrator, "MAX_TEAM_ROUNDS")
        assert not hasattr(orchestrator, "MAX_RETRIES")

    def test_project_lead_agent_module_removed(self):
        with pytest.raises(ModuleNotFoundError):
            __import__("harness.team.project_lead_agent")


# ============================================================================
# P2-12: 成员状态保鲜
# ============================================================================

class TestTeamContextFreshness:
    def test_refresh_team_context(self, deps):
        from harness.team.models import TeammateStatus

        async def _test():
            from harness.team.orchestrator import TeamOrchestrator

            alice = _make_agent("alice", deps)
            orch = TeamOrchestrator.__new__(TeamOrchestrator)
            orch.team_context = deps["ctx"]
            orch.teammates = {"alice": alice}

            alice.status = TeammateStatus.WORKING
            orch._refresh_team_context()

            assert len(deps["ctx"].members) == 1
            assert deps["ctx"].members[0].agent_name == "alice"
            assert deps["ctx"].members[0].status == TeammateStatus.WORKING

            # 动态 spawn 的新成员也进入快照
            bob = _make_agent("bob", deps)
            bob.status = TeammateStatus.IDLE
            orch.teammates["bob"] = bob
            orch._refresh_team_context()
            assert {m.agent_name for m in deps["ctx"].members} == {"alice", "bob"}

        asyncio.run(_test())

    def test_system_prompt_reflects_latest_members(self, deps):
        from harness.team.models import TeamMemberRuntime, TeammateStatus

        agent = _make_agent("alice", deps)
        # 初始 prompt 不含 bob
        assert "bob" not in agent._system_prompt

        # 新成员加入后重建 prompt (work_loop 每轮做的事)
        deps["ctx"].members.append(
            TeamMemberRuntime(agent_name="bob", role="member", status=TeammateStatus.IDLE))
        agent._system_prompt = agent._build_system_prompt()

        assert "bob" in agent._system_prompt


# ============================================================================
# P2-14: cancel_stale_tasks
# ============================================================================

class TestCancelStaleTasks:
    def test_only_team_nonterminal_cancelled(self, deps):
        from harness.team.models import TeamTaskStatus

        async def _test():
            store = deps["store"]
            # 上一轮团队遗留 (非终态)
            t1 = await store.create_task(title="遗留 pending")
            t2 = await store.create_task(title="遗留 in_progress")
            await store.update_task(t2.id, status=TeamTaskStatus.IN_PROGRESS)
            # 用户手工创建
            t3 = await store.create_task(title="用户任务", origin="user")
            # 已完成的团队任务 (终态, 不动)
            t4 = await store.create_task(title="已完成")
            await store.update_task(t4.id, status=TeamTaskStatus.COMPLETED)

            cancelled = await store.cancel_stale_tasks()
            cancelled_ids = {t.id for t in cancelled}

            assert cancelled_ids == {t1.id, t2.id}
            for t in cancelled:
                assert t.status == TeamTaskStatus.CANCELLED
                assert "遗留" in (t.error or "")

            t3r = await store.get_task(t3.id)
            assert t3r.status == TeamTaskStatus.PENDING  # 用户任务保留
            t4r = await store.get_task(t4.id)
            assert t4r.status == TeamTaskStatus.COMPLETED

        asyncio.run(_test())

    def test_origin_defaults_to_team(self, deps):
        async def _test():
            store = deps["store"]
            task = await store.create_task(title="默认来源")
            assert task.origin == "team"

        asyncio.run(_test())
