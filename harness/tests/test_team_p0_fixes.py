"""P0 修复回归测试.

覆盖:
  P0-1: 移除 idle 自毁; 死成员任务回池; 崩溃成员检测 + 任务有界重试
  P0-2: assign_task 受理/拒绝语义; orchestrator 先受理后落账 + 失败回滚
  P0-3: request_plan_approval 定向发送给 Lead; 无 Lead 时安全失败

注意: 除 idle/崩溃测试外, 均不 spawn agent loop — 直接设置 status,
避免后台 loop 与被测断言竞争 (spawn 的 agent 看到 WORKING 会立刻进 _work_loop).

运行:  cd multiagent-studio && python -m pytest harness/tests/test_team_p0_fixes.py -v
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

logging.getLogger("harness.team").setLevel(logging.WARNING)


# ── 辅助: 允许 TaskStore/MessageBus 使用自定义目录 (与 test_team_flow.py 相同) ──

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
    """创建 TeammateAgent 所需的全部依赖."""
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
    )


def _bare_orchestrator(store, bus, teammates):
    """绕过 __init__ 构造最小可用 Orchestrator (不触碰真实数据目录)."""
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
# P0-1: IDLE 自毁移除 & 高可用调度
# ============================================================================

class TestIdleNoSelfDestruct:
    """P0-1: 成员 IDLE 再久也不应自行 SHUTDOWN (生命周期由 Orchestrator 统一管理)."""

    def test_idle_member_never_self_destructs(self, deps, monkeypatch):
        from harness.team.models import TeammateStatus

        # 把轮询间隔缩到 10ms — 0.3s ≈ 30 轮, 远超旧的 12 轮自毁阈值
        monkeypatch.setattr("harness.team.teammate_agent.IDLE_POLL_INTERVAL", 0.01)

        async def _test():
            agent = _make_agent("alice", deps)
            await agent.spawn()
            await asyncio.sleep(0.3)

            assert agent.status == TeammateStatus.IDLE
            assert agent._should_exit is False
            assert agent._task is not None and not agent._task.done()

            await agent.shutdown()
            assert agent.status == TeammateStatus.SHUTDOWN

        asyncio.run(_test())


class TestDispatchHighAvailability:
    """P0-1: dispatch 绕开不可用成员, 任务不卡死."""

    def test_task_of_dead_member_returns_to_pool(self, deps):
        """指定成员不存在/已退出 → 任务回池, 分给其他 IDLE 成员."""
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.IDLE

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})

            # 任务指定给一个不存在 ("已死") 的成员
            task = await deps["store"].create_task(title="孤儿任务", assigned_agent="ghost")
            dispatched = await orch._dispatch_ready_tasks()

            assert dispatched == 1
            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.IN_PROGRESS
            assert reloaded.assigned_agent == "alice"
            assert alice.status == TeammateStatus.WORKING

        asyncio.run(_test())

    def test_task_of_busy_member_waits_for_next_round(self, deps):
        """指定成员正忙 → 跳过, 任务保持 PENDING 等下轮."""
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.WORKING

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})

            task = await deps["store"].create_task(title="排队任务", assigned_agent="alice")
            dispatched = await orch._dispatch_ready_tasks()

            assert dispatched == 0
            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.PENDING
            assert reloaded.assigned_agent == "alice"

        asyncio.run(_test())


class TestCrashReaper:
    """P0-1: 看门狗回收崩溃成员, 任务有界重试."""

    async def _simulate_crash(self, agent, store):
        """spawn 后杀掉 agent loop, 模拟 WORKING 中崩溃."""
        from harness.team.models import TeamTaskStatus, TeammateStatus

        await agent.spawn()
        await asyncio.sleep(0.05)
        agent._task.cancel()
        await asyncio.sleep(0.05)
        assert agent._task.done()

        task = await store.create_task(title="崩溃任务", assigned_agent=agent.name)
        await store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
        agent.status = TeammateStatus.WORKING
        agent.current_task_id = task.id
        return task

    def test_requeue_in_progress_task_of_crashed_member(self, deps):
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            task = await self._simulate_crash(alice, deps["store"])

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})
            await orch._reap_crashed_teammates()

            assert alice.status == TeammateStatus.FAILED
            assert alice.current_task_id is None
            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.PENDING
            assert reloaded.assigned_agent is None
            assert reloaded.retry_count == 1
            # 进度事件被唤醒, 调度循环可立即重派
            assert orch._progress_event.is_set()

        asyncio.run(_test())

    def test_fail_task_when_max_retries_exceeded(self, deps):
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            task = await self._simulate_crash(alice, deps["store"])
            await deps["store"].update_task(
                task.id, retry_count=task.max_retries, status=TeamTaskStatus.IN_PROGRESS)

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})
            await orch._reap_crashed_teammates()

            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.FAILED
            assert "最大重试" in (reloaded.error or "")

        asyncio.run(_test())


# ============================================================================
# P0-2: assign_task 受理语义 + 先受理后落账
# ============================================================================

class TestAssignTaskSemantics:
    """P0-2: assign_task 返回受理结果, 拒绝时 orchestrator 回滚任务状态."""

    def test_accepts_when_idle(self, deps):
        from harness.team.models import TeamTask, TeammateStatus

        async def _test():
            agent = _make_agent("alice", deps)
            agent.status = TeammateStatus.IDLE

            task = TeamTask(project_id="test_proj", title="测试任务")
            accepted = await agent.assign_task(task)

            assert accepted is True
            assert agent.status == TeammateStatus.WORKING
            assert agent.current_task_id == task.id

        asyncio.run(_test())

    def test_rejects_when_not_idle(self, deps):
        from harness.team.models import TeamTask, TeammateStatus

        async def _test():
            agent = _make_agent("alice", deps)
            agent.status = TeammateStatus.WORKING

            task = TeamTask(project_id="test_proj", title="测试任务")
            accepted = await agent.assign_task(task)

            assert accepted is False
            assert agent.current_task_id is None  # 未被污染

        asyncio.run(_test())

    def test_orchestrator_assign_success_commits(self, deps):
        """受理成功 → 落账 IN_PROGRESS + 发 SSE."""
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.IDLE

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})
            task = await deps["store"].create_task(title="正常任务")

            ok = await orch._assign_task_to_teammate(alice, task)

            assert ok is True
            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.IN_PROGRESS
            assert reloaded.assigned_agent == "alice"
            assert orch._event_queue.qsize() == 2  # task_update + member_status

        asyncio.run(_test())

    def test_orchestrator_assign_reject_rolls_back(self, deps):
        """受理失败 (竞态) → 任务回滚为未分配 PENDING, 不卡 IN_PROGRESS."""
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = _make_agent("alice", deps)
            alice.status = TeammateStatus.WORKING  # 模拟竞态: 刚认领了别的任务

            orch = _bare_orchestrator(deps["store"], deps["bus"], {"alice": alice})
            task = await deps["store"].create_task(title="竞态任务")

            ok = await orch._assign_task_to_teammate(alice, task)

            assert ok is False
            reloaded = await deps["store"].get_task(task.id)
            assert reloaded.status == TeamTaskStatus.PENDING
            assert reloaded.assigned_agent is None
            assert orch._event_queue.qsize() == 0  # 失败不发 SSE

        asyncio.run(_test())


# ============================================================================
# P0-3: 审批请求定向发送给 Lead
# ============================================================================

class TestPlanApprovalDirected:
    """P0-3: request_plan_approval 只发给 Lead, 不广播."""

    def test_goes_to_lead_only(self, deps):
        from harness.team.models import TeamMessageType
        from harness.team.tools import create_team_tools, set_current_agent

        async def _test():
            bus = deps["bus"]
            for name in ("lead", "alice", "bob"):
                bus.register_agent(name)

            tools = create_team_tools(message_bus=bus, role="member", lead_name="lead")
            tool = next(t for t in tools if t.name == "request_plan_approval")

            set_current_agent("alice")
            result = await tool.ainvoke({"plan_description": "删除生产数据库"})

            assert "lead" in result
            lead_msgs = await bus.read_inbox("lead")
            assert len(lead_msgs) == 1
            assert lead_msgs[0].msg_type == TeamMessageType.PLAN_APPROVAL_REQUEST
            assert lead_msgs[0].from_agent == "alice"
            assert lead_msgs[0].to_agent == "lead"
            # 其他成员不应收到
            assert await bus.read_inbox("alice") == []
            assert await bus.read_inbox("bob") == []

        asyncio.run(_test())

    def test_fails_safely_without_lead(self, deps):
        from harness.team.tools import (
            create_team_tools, set_current_agent, set_current_agent_instance,
        )

        async def _test():
            bus = deps["bus"]
            for name in ("alice", "bob"):
                bus.register_agent(name)

            tools = create_team_tools(message_bus=bus, role="member")  # 无 lead_name
            tool = next(t for t in tools if t.name == "request_plan_approval")

            set_current_agent("alice")
            set_current_agent_instance(None)  # 确保无实例 fallback
            result = await tool.ainvoke({"plan_description": "危险操作"})

            assert "Error" in result
            assert await bus.read_inbox("alice") == []
            assert await bus.read_inbox("bob") == []

        asyncio.run(_test())
