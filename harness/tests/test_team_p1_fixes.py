"""P1 修复回归测试.

覆盖:
  P1-4: task_store.claim 原子认领 (CAS) — 双认领/非 pending/错误 assignee/依赖门禁
  P1-6: 成败分开记账; 协议违规检测 (跑完未 task_update → FAILED)
  P1-7: propagate_failures 级联取消 (含链式传播与幂等)

运行:  cd multiagent-studio && python -m pytest harness/tests/test_team_p1_fixes.py -v
"""

from __future__ import annotations

import asyncio
import logging
import types
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


def _bare_orchestrator(store, bus, teammates):
    from harness.team.orchestrator import TeamOrchestrator

    orch = TeamOrchestrator.__new__(TeamOrchestrator)
    orch._project_id = "test_proj"
    orch._thread_id = "thread-1"
    orch._user_id = "default"
    orch.task_store = store
    orch.message_bus = bus
    orch.teammates = teammates
    orch._event_queue = asyncio.Queue()
    orch._progress_event = asyncio.Event()
    return orch


# ============================================================================
# P1-4: 原子认领 (CAS)
# ============================================================================

class TestAtomicClaim:
    """task_store.claim: 全部认领条件在 flock 内校验."""

    def test_claim_only_one_wins(self, deps):
        async def _test():
            store = deps["store"]
            task = await store.create_task(title="竞争任务")

            first = await store.claim(task.id, "alice")
            second = await store.claim(task.id, "bob")

            assert first is not None and first.assigned_agent == "alice"
            assert second is None  # 后到者失败

            reloaded = await store.get_task(task.id)
            assert reloaded.assigned_agent == "alice"
            assert reloaded.status.value == "in_progress"

        asyncio.run(_test())

    def test_claim_rejects_non_pending_and_wrong_assignee(self, deps):
        async def _test():
            store = deps["store"]
            task = await store.create_task(title="指定任务", assigned_agent="bob")

            # 他人不能认领已分配给 bob 的任务
            assert await store.claim(task.id, "alice") is None
            # 本人可以认领 (Tier1 场景)
            assert await store.claim(task.id, "bob") is not None
            # 已 IN_PROGRESS, 不可再认领
            assert await store.claim(task.id, "bob") is None

        asyncio.run(_test())

    def test_claim_respects_dependencies(self, deps):
        async def _test():
            store = deps["store"]
            a = await store.create_task(title="上游任务")
            b = await store.create_task(title="下游任务", dependencies=[a.id])

            # 依赖未就绪 → 不可认领
            assert await store.claim(b.id, "alice") is None
            # 依赖完成 → 可认领
            from harness.team.models import TeamTaskStatus
            await store.update_task(a.id, status=TeamTaskStatus.COMPLETED)
            assert await store.claim(b.id, "alice") is not None

        asyncio.run(_test())

    def test_orchestrator_assign_loses_to_member_claim(self, deps):
        """成员先认领成功 → orchestrator 派单 CAS 失败, 无双执行."""
        from harness.team.models import TeammateStatus

        async def _test():
            store = deps["store"]
            bob = _make_agent("bob", deps)
            bob.status = TeammateStatus.IDLE

            task = await store.create_task(title="撞车任务")
            # 成员侧先认领成功
            assert await store.claim(task.id, "alice") is not None

            orch = _bare_orchestrator(store, deps["bus"], {"bob": bob})
            ok = await orch._assign_task_to_teammate(bob, task)

            assert ok is False
            assert bob.status == TeammateStatus.IDLE  # 未被唤醒
            assert orch._event_queue.qsize() == 0
            reloaded = await store.get_task(task.id)
            assert reloaded.assigned_agent == "alice"  # 归属不变

        asyncio.run(_test())

    def test_claim_task_tool_guard_when_busy(self, deps):
        """claim_task 工具: 在手任务未完成时拒绝再认领."""
        from harness.team.tools import (
            create_team_tools, set_current_agent, set_current_agent_instance,
        )

        async def _test():
            store = deps["store"]
            task = await store.create_task(title="待认领任务")

            tools = create_team_tools(task_store=store, role="member")
            tool = next(t for t in tools if t.name == "claim_task")

            set_current_agent("alice")
            set_current_agent_instance(types.SimpleNamespace(current_task_id="别的任务"))
            try:
                result = await tool.ainvoke({"task_id": task.id})
            finally:
                set_current_agent_instance(None)

            assert "请先完成" in result
            reloaded = await store.get_task(task.id)
            assert reloaded.status.value == "pending"
            assert reloaded.assigned_agent is None

        asyncio.run(_test())


# ============================================================================
# P1-6: 成败记账 + 协议违规检测
# ============================================================================

class TestSettlement:
    """work_loop 尾部结算: 以任务板为准, 不盲信 LLM 跑完."""

    async def _wait_idle(self, agent, timeout: float = 3.0):
        from harness.team.models import TeammateStatus
        waited = 0.0
        while agent.status != TeammateStatus.IDLE and waited < timeout:
            await asyncio.sleep(0.05)
            waited += 0.05
        return agent.status == TeammateStatus.IDLE

    def test_protocol_violation_marks_task_failed(self, deps):
        """fake LLM 跑完但没调 task_update → 任务置 FAILED, failed_tasks+1."""
        from harness.team.models import TeamTaskStatus

        async def _test():
            store = deps["store"]
            agent = _make_agent("alice", deps)
            await agent.spawn()
            await asyncio.sleep(0.05)

            # 模拟 dispatch: 任务先置 IN_PROGRESS 再派给 alice
            task = await store.create_task(title="违规任务", assigned_agent="alice")
            await store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
            assert await agent.assign_task(task) is True

            assert await self._wait_idle(agent)

            reloaded = await store.get_task(task.id)
            assert reloaded.status == TeamTaskStatus.FAILED
            assert "协议违规" in (reloaded.error or "")
            assert agent.failed_tasks == 1
            assert agent.completed_tasks == 0

            await agent.shutdown()

        asyncio.run(_test())

    def test_work_loop_exception_counts_as_failed(self, deps, monkeypatch):
        """work_loop 抛异常 → failed_tasks+1, completed_tasks 不变."""
        from harness.team.models import TeamTaskStatus

        def _boom(*args, **kwargs):
            raise RuntimeError("LLM 服务不可用")

        monkeypatch.setattr("langchain.agents.create_agent", _boom)

        async def _test():
            store = deps["store"]
            agent = _make_agent("alice", deps)
            await agent.spawn()
            await asyncio.sleep(0.05)

            task = await store.create_task(title="异常任务", assigned_agent="alice")
            await store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
            assert await agent.assign_task(task) is True

            assert await self._wait_idle(agent)

            reloaded = await store.get_task(task.id)
            assert reloaded.status == TeamTaskStatus.FAILED
            assert "LLM 服务不可用" in (reloaded.error or "")
            assert agent.failed_tasks == 1
            assert agent.completed_tasks == 0
            assert agent.last_error is not None

            await agent.shutdown()

        asyncio.run(_test())


# ============================================================================
# P1-7: 依赖失败级联取消
# ============================================================================

class TestPropagateFailures:
    """propagate_failures: 终态失败沿依赖链传播, 幂等."""

    def test_cascades_along_dependency_chain(self, deps):
        from harness.team.models import TeamTaskStatus

        async def _test():
            store = deps["store"]
            a = await store.create_task(title="A")
            b = await store.create_task(title="B", dependencies=[a.id])
            c = await store.create_task(title="C", dependencies=[b.id])
            d = await store.create_task(title="D (独立)")

            await store.update_task(a.id, status=TeamTaskStatus.FAILED, error="炸了")

            cancelled = await store.propagate_failures()
            cancelled_ids = {t.id for t in cancelled}

            assert cancelled_ids == {b.id, c.id}  # 链式传播
            for t in cancelled:
                assert t.status == TeamTaskStatus.CANCELLED
                assert t.error

            d_reloaded = await store.get_task(d.id)
            assert d_reloaded.status == TeamTaskStatus.PENDING

            # 幂等: 再次调用无新增
            assert await store.propagate_failures() == []

        asyncio.run(_test())

    def test_does_not_touch_task_with_retrying_dependency(self, deps):
        """依赖被回收重试 (回滚 PENDING) 时, 下游不取消."""
        async def _test():
            store = deps["store"]
            a = await store.create_task(title="A")
            b = await store.create_task(title="B", dependencies=[a.id])
            # A 被回收重试 → 仍是 PENDING, 不是终态失败
            assert await store.propagate_failures() == []
            b_reloaded = await store.get_task(b.id)
            assert b_reloaded.status.value == "pending"

        asyncio.run(_test())


# ============================================================================
# P1-8: 认领候选集修正
# ============================================================================

class TestClaimCandidateSet:
    """_maybe_claim_task: Tier1 (分配给我) 可达, 他人任务不抢."""

    def test_tier1_claims_task_assigned_to_me(self, deps):
        from harness.team.models import TeammateStatus

        async def _test():
            store = deps["store"]
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.IDLE
            alice.enable_auto_claim()

            task = await store.create_task(title="我的任务", assigned_agent="alice")
            claimed = await alice._maybe_claim_task()

            assert claimed is True
            assert alice.status == TeammateStatus.WORKING
            assert alice.current_task_id == task.id
            reloaded = await store.get_task(task.id)
            assert reloaded.status.value == "in_progress"
            assert reloaded.assigned_agent == "alice"

        asyncio.run(_test())

    def test_skips_task_assigned_to_others(self, deps):
        from harness.team.models import TeammateStatus

        async def _test():
            store = deps["store"]
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.IDLE
            alice.enable_auto_claim()

            task = await store.create_task(title="别人的任务", assigned_agent="bob")
            claimed = await alice._maybe_claim_task()

            assert claimed is False
            assert alice.status == TeammateStatus.IDLE
            reloaded = await store.get_task(task.id)
            assert reloaded.status.value == "pending"
            assert reloaded.assigned_agent == "bob"

        asyncio.run(_test())
