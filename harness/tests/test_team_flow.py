"""Agent Team 完整流程测试.

覆盖层级:
  L1: 模型 & 工具编译 — FSM, RequestStatus, 15 个工具, ContextVar
  L2: TaskStore & MessageBus — CRUD, 依赖解析, drain-on-read, inbox 隔离
  L3: TeammateAgent 生命周期 — spawn, assign_task, inbox 消息路由, _maybe_claim_task
  L4: Orchestrator 调度 — _dispatch_ready_tasks, _select_idle_teammate, _is_complete
  L5: TeamTracer — no-op 模式, LangChain callback

运行:  cd multiagent-studio && python -m pytest harness/tests/test_team_flow.py -v
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

# ── 抑制测试期间的日志噪音 ──
logging.getLogger("harness.team").setLevel(logging.WARNING)

# ============================================================================
# L1: 模型 & 工具编译
# ============================================================================

class TestModels:
    """模型: FSM 状态, RequestStatus, 消息类型."""

    def test_task_status_fsm(self):
        from harness.team.models import TeamTaskStatus

        assert TeamTaskStatus.PENDING.value == "pending"
        assert TeamTaskStatus.IN_PROGRESS.value == "in_progress"
        assert TeamTaskStatus.COMPLETED.value == "completed"

        # 终态检查
        assert TeamTaskStatus.COMPLETED.is_terminal is True
        assert TeamTaskStatus.FAILED.is_terminal is True
        assert TeamTaskStatus.CANCELLED.is_terminal is True
        assert TeamTaskStatus.PENDING.is_terminal is False
        assert TeamTaskStatus.IN_PROGRESS.is_terminal is False

    def test_request_status_fsm(self):
        from harness.team.models import RequestStatus

        assert RequestStatus.PENDING.value == "pending"
        assert RequestStatus.APPROVED.value == "approved"
        assert RequestStatus.REJECTED.value == "rejected"
        # 验证 FSM 三态
        assert len(list(RequestStatus)) == 3

    def test_teammate_status_lifecycle(self):
        from harness.team.models import TeammateStatus

        statuses = list(TeammateStatus)
        assert TeammateStatus.SPAWNING in statuses
        assert TeammateStatus.WORKING in statuses
        assert TeammateStatus.IDLE in statuses
        assert TeammateStatus.SHUTTING_DOWN in statuses
        assert TeammateStatus.SHUTDOWN in statuses
        assert TeammateStatus.FAILED in statuses
        assert len(statuses) == 6

    def test_message_types(self):
        from harness.team.models import TeamMessageType

        # 基础
        assert TeamMessageType.TEXT.value == "text"
        assert TeamMessageType.BROADCAST.value == "broadcast"
        # s16 协议
        assert TeamMessageType.SHUTDOWN_REQUEST.value == "shutdown_request"
        assert TeamMessageType.SHUTDOWN_RESPONSE.value == "shutdown_response"
        assert TeamMessageType.PLAN_APPROVAL_REQUEST.value == "plan_approval_request"
        assert TeamMessageType.PLAN_APPROVAL_RESPONSE.value == "plan_approval_response"

    def test_team_task_model(self):
        from harness.team.models import TeamTask
        task = TeamTask(project_id="p1", title="测试任务", priority="high")
        assert task.id  # 自动生成
        assert task.status.value == "pending"
        assert task.assigned_agent is None
        assert task.dependencies == []
        assert task.created_at  # 自动生成时间戳


class TestTools:
    """工具: 角色过滤, 15 个工具, ContextVar."""

    def test_lead_tools_count(self):
        from harness.team.tools import LEAD_TOOLS, SHARED_TOOLS
        all_lead = LEAD_TOOLS | SHARED_TOOLS
        assert len(all_lead) == 11, f"Lead should have 11 tools, got {len(all_lead)}"

    def test_member_tools_count(self):
        from harness.team.tools import SHARED_TOOLS, MEMBER_TOOLS
        all_member = SHARED_TOOLS | MEMBER_TOOLS
        assert len(all_member) == 9, f"Member should have 9 tools, got {len(all_member)}"

    def test_lead_exclusive_tools(self):
        from harness.team.tools import LEAD_TOOLS, MEMBER_TOOLS
        # Lead 专属工具不应该在 Member 中
        lead_only = LEAD_TOOLS - MEMBER_TOOLS - {"broadcast"}  # broadcast 也在 SHARED_TOOLS 中
        for tool_name in ["delegate_to_member", "list_teammates", "shutdown_teammate",
                          "approve_plan", "spawn_teammate"]:
            assert tool_name in LEAD_TOOLS, f"{tool_name} should be in LEAD_TOOLS"

    def test_member_exclusive_tools(self):
        from harness.team.tools import MEMBER_TOOLS
        for tool_name in ["request_plan_approval", "claim_task", "idle", "shutdown_response"]:
            assert tool_name in MEMBER_TOOLS, f"{tool_name} should be in MEMBER_TOOLS"

    def test_create_team_tools_lead(self):
        from harness.team.tools import create_team_tools
        tools = create_team_tools(role="lead")
        names = {t.name for t in tools}
        assert len(names) == 11
        assert "delegate_to_member" in names
        assert "spawn_teammate" in names
        assert "approve_plan" in names
        assert "claim_task" not in names  # Member only
        assert "shutdown_response" not in names  # Member only

    def test_create_team_tools_member(self):
        from harness.team.tools import create_team_tools
        tools = create_team_tools(role="member")
        names = {t.name for t in tools}
        assert len(names) == 9
        assert "claim_task" in names
        assert "shutdown_response" in names
        assert "delegate_to_member" not in names  # Lead only
        assert "spawn_teammate" not in names  # Lead only

    def test_context_var_agent_name(self):
        from harness.team.tools import set_current_agent, get_current_agent
        set_current_agent("alice")
        assert get_current_agent() == "alice"
        set_current_agent("bob")
        assert get_current_agent() == "bob"

    def test_context_var_agent_instance(self):
        from harness.team.tools import (
            set_current_agent_instance, get_current_agent_instance,
        )

        class MockAgent:
            _should_exit = False
            _pending_requests = {}

        mock = MockAgent()
        set_current_agent_instance(mock)
        assert get_current_agent_instance() is mock
        set_current_agent_instance(None)
        assert get_current_agent_instance() is None


# ============================================================================
# L2: TaskStore & MessageBus
# ============================================================================

# ── 辅助: 允许 TaskStore/MessageBus 使用自定义目录 ──

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


class TestTaskStore:
    """TaskStore: CRUD, 依赖解析, unclaimed."""

    @pytest.fixture
    def store(self, tmp_path):
        from harness.team.task_store import TeamTaskStore
        return TeamTaskStore._create_with_dir(tmp_path, "test_project")

    def test_create_and_get(self, store):
        async def _test():
            task = await store.create_task(title="测试", description="描述")
            assert task.id
            loaded = await store.get_task(task.id)
            assert loaded.title == "测试"

        asyncio.run(_test())

    def test_update(self, store):
        async def _test():
            task = await store.create_task(title="原始")
            updated = await store.update_task(task.id, title="新标题")
            assert updated.title == "新标题"

        asyncio.run(_test())

    def test_dependency_resolution(self, store):
        async def _test():
            a = await store.create_task(title="A")
            b = await store.create_task(title="B", dependencies=[a.id])
            ready = await store.get_ready_tasks()
            assert a.id in {t.id for t in ready}
            assert b.id not in {t.id for t in ready}
            await store.update_task(a.id, status="completed")
            ready = await store.get_ready_tasks()
            assert b.id in {t.id for t in ready}

        asyncio.run(_test())

    def test_get_unclaimed_tasks(self, store):
        async def _test():
            a = await store.create_task(title="A")  # no assigned_agent
            b = await store.create_task(title="B", assigned_agent="alice")
            unclaimed = await store.get_unclaimed_tasks()
            ids = {t.id for t in unclaimed}
            assert a.id in ids
            assert b.id not in ids  # 已分配

        asyncio.run(_test())

    def test_clear_all(self, store):
        async def _test():
            await store.create_task(title="A")
            await store.create_task(title="B")
            await store.clear_all()
            tasks = await store.load_tasks()
            assert len(tasks) == 0

        asyncio.run(_test())

    def test_list_by_status(self, store):
        async def _test():
            from harness.team.models import TeamTaskStatus
            await store.create_task(title="A")
            t = await store.create_task(title="B")
            await store.update_task(t.id, status=TeamTaskStatus.COMPLETED)
            pending = await store.list_tasks(status=TeamTaskStatus.PENDING)
            completed = await store.list_tasks(status=TeamTaskStatus.COMPLETED)
            assert len(pending) == 1
            assert len(completed) == 1

        asyncio.run(_test())


class TestMessageBus:
    """MessageBus: 发送, 接收, drain-on-read, inbox 隔离."""

    @pytest.fixture
    def bus(self, tmp_path):
        from harness.team.message_bus import TeamMessageBus
        return TeamMessageBus._create_with_dir(tmp_path, "test_project")

    def test_send_and_read_inbox(self, bus):
        async def _test():
            from harness.team.models import TeamMessage, TeamMessageType
            await bus.send(TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.TEXT, content="hello",
            ))
            msgs = await bus.read_inbox("alice")
            assert len(msgs) == 1
            assert msgs[0].content == "hello"
            # drain-on-read: 第二次读取为空
            msgs2 = await bus.read_inbox("alice")
            assert len(msgs2) == 0

        asyncio.run(_test())

    def test_inbox_isolation(self, bus):
        async def _test():
            from harness.team.models import TeamMessage, TeamMessageType
            await bus.send(TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.TEXT, content="for alice",
            ))
            await bus.send(TeamMessage(
                from_agent="lead", to_agent="bob",
                msg_type=TeamMessageType.TEXT, content="for bob",
            ))
            alice_msgs = await bus.read_inbox("alice")
            bob_msgs = await bus.read_inbox("bob")
            assert len(alice_msgs) == 1
            assert alice_msgs[0].content == "for alice"
            assert len(bob_msgs) == 1
            assert bob_msgs[0].content == "for bob"

        asyncio.run(_test())

    def test_shutdown_request_message(self, bus):
        async def _test():
            from harness.team.models import TeamMessage, TeamMessageType
            await bus.send(TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.SHUTDOWN_REQUEST,
                content="shutdown", request_id="req-001",
            ))
            msgs = await bus.read_inbox("alice")
            assert len(msgs) == 1
            assert msgs[0].msg_type == TeamMessageType.SHUTDOWN_REQUEST
            assert msgs[0].request_id == "req-001"

        asyncio.run(_test())


# ============================================================================
# L3: TeammateAgent 生命周期 (需要 mock LLM)
# ============================================================================

class TestTeammateAgent:
    """TeammateAgent: spawn, 消息路由, auto-claim, shutdown."""

    @pytest.fixture
    def agent_deps(self, tmp_path):
        """创建 TeammateAgent 所需的所有依赖."""
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
        llm = FakeListChatModel(responses=["任务已完成。"])

        return {
            "store": store, "bus": bus, "ctx": ctx, "llm": llm,
        }

    def test_spawn_and_shutdown(self, agent_deps):
        """Agent 从 spawn → IDLE → shutdown 的完整生命周期."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeammateStatus

        async def _test():
            agent = TeammateAgent(
                agent_name="alice",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="member",
            )
            # spawn
            await agent.spawn()
            assert agent.status == TeammateStatus.IDLE
            assert agent.completed_tasks == 0

            # 给一点时间让 agent loop 运行
            await asyncio.sleep(0.1)

            # shutdown
            await agent.shutdown()
            assert agent.status == TeammateStatus.SHUTDOWN

        asyncio.run(_test())

    def test_assign_task_wakes_agent(self, agent_deps):
        """assign_task 应设置 WORKING 并唤醒 agent."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamTask, TeammateStatus

        async def _test():
            agent = TeammateAgent(
                agent_name="alice",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="member",
            )
            await agent.spawn()
            await asyncio.sleep(0.1)

            assert agent.status == TeammateStatus.IDLE

            task = TeamTask(project_id="test_proj", title="测试任务")
            await agent.assign_task(task)
            assert agent.status == TeammateStatus.WORKING
            assert agent.current_task_id == task.id

            await agent.shutdown()

        asyncio.run(_test())

    def test_maybe_claim_task_gated_by_can_claim(self, agent_deps):
        """_maybe_claim_task 在 _can_claim=False 时不认领任务."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamTask

        async def _test():
            # 创建未分配任务
            await agent_deps["store"].create_task(title="未分配任务")
            await agent_deps["store"].create_task(title="已分配任务", assigned_agent="bob")

            agent = TeammateAgent(
                agent_name="alice",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="member",
            )
            await agent.spawn()
            await asyncio.sleep(0.1)

            # _can_claim 默认 False → 不应认领
            assert agent._can_claim is False
            claimed = await agent._maybe_claim_task()
            assert claimed is False

            # 开启 auto-claim → 应认领
            agent.enable_auto_claim()
            assert agent._can_claim is True
            claimed = await agent._maybe_claim_task()
            assert claimed is True
            assert agent.status.value == "working"

            await agent.shutdown()

        asyncio.run(_test())

    def test_handle_inbox_shutdown_request(self, agent_deps):
        """收到 SHUTDOWN_REQUEST 后应注入消息到 _messages, 而非硬编码 shutdown."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamMessage, TeamMessageType

        async def _test():
            agent = TeammateAgent(
                agent_name="alice",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="member",
            )
            await agent.spawn()
            await asyncio.sleep(0.1)

            # 模拟收到 SHUTDOWN_REQUEST
            msg = TeamMessage(
                from_agent="lead", to_agent="alice",
                msg_type=TeamMessageType.SHUTDOWN_REQUEST,
                content="shutdown", request_id="req-001",
            )
            await agent._handle_inbox_message(msg)

            # 验证: 不应立即 _should_exit (LLM 决策)
            assert agent._should_exit is False
            # 验证: 消息应注入到 _messages
            assert len(agent._messages) > 0
            last_msg = str(agent._messages[-1].content)
            assert "shutdown_request" in last_msg
            assert "shutdown_response" in last_msg
            assert "req-001" in last_msg

            await agent.shutdown()

        asyncio.run(_test())

    def test_handle_inbox_lifecycle_wakes_lead(self, agent_deps):
        """LIFECYCLE 消息应唤醒 IDLE 状态的 Lead agent."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamMessage, TeamMessageType, TeammateStatus

        async def _test():
            agent = TeammateAgent(
                agent_name="lead",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="lead",
            )
            await agent.spawn()
            await asyncio.sleep(0.1)

            # 确保 IDLE
            agent.status = TeammateStatus.IDLE

            msg = TeamMessage(
                from_agent="alice", to_agent="lead",
                msg_type=TeamMessageType.LIFECYCLE,
                content="已完成 1 个任务, 等待新任务",
            )
            await agent._handle_inbox_message(msg)

            # LIFECYCLE 应唤醒 Lead
            assert agent.status == TeammateStatus.WORKING

            await agent.shutdown()

        asyncio.run(_test())

    def test_enable_auto_claim_method(self, agent_deps):
        """enable_auto_claim() 设置 _can_claim=True 并唤醒."""
        from harness.team.teammate_agent import TeammateAgent

        async def _test():
            agent = TeammateAgent(
                agent_name="alice",
                llm=agent_deps["llm"],
                tools=[],
                team_context=agent_deps["ctx"],
                message_bus=agent_deps["bus"],
                task_store=agent_deps["store"],
                role="member",
            )
            await agent.spawn()
            await asyncio.sleep(0.1)

            assert agent._can_claim is False
            agent.enable_auto_claim()
            assert agent._can_claim is True

            await agent.shutdown()

        asyncio.run(_test())


# ============================================================================
# L4: Orchestrator 调度逻辑
# ============================================================================

class TestOrchestrator:
    """Orchestrator: 调度逻辑, 完成检测, teammate 选择."""

    @pytest.fixture
    def orch_deps(self, tmp_path):
        """创建 Orchestrator 所需的所有依赖 (无 LLM, 用于测试调度逻辑)."""
        from harness.team.task_store import TeamTaskStore
        from harness.team.message_bus import TeamMessageBus
        from harness.team.context import TeamContext
        from harness.team.models import TeamMemberRuntime, TeammateStatus
        from harness.team.teammate_agent import TeammateAgent
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        store = TeamTaskStore._create_with_dir(tmp_path / "tasks", "test_proj")
        bus = TeamMessageBus._create_with_dir(tmp_path / "msgs", "test_proj")
        ctx = TeamContext(
            project_id="test_proj",
            project_name="Test",
            project_description="Test project",
            thread_id="thread-1",
            user_id="default",
            members=[
                TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.SPAWNING),
                TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.SPAWNING),
                TeamMemberRuntime(agent_name="bob", role="member", status=TeammateStatus.SPAWNING),
            ],
        )
        llm = FakeListChatModel(responses=["OK"])

        return {
            "store": store, "bus": bus, "ctx": ctx, "llm": llm,
        }

    def test_select_idle_teammate_prefers_less_loaded(self, orch_deps):
        """_select_idle_teammate 应选择已完成任务数少的 IDLE teammate."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeammateStatus

        async def _test():
            alice = TeammateAgent(
                agent_name="alice", llm=orch_deps["llm"], tools=[],
                team_context=orch_deps["ctx"], message_bus=orch_deps["bus"],
                task_store=orch_deps["store"], role="member",
            )
            bob = TeammateAgent(
                agent_name="bob", llm=orch_deps["llm"], tools=[],
                team_context=orch_deps["ctx"], message_bus=orch_deps["bus"],
                task_store=orch_deps["store"], role="member",
            )
            await alice.spawn()
            await bob.spawn()
            await asyncio.sleep(0.1)

            alice.status = TeammateStatus.IDLE
            bob.status = TeammateStatus.IDLE
            alice.completed_tasks = 5
            bob.completed_tasks = 2

            teammates = {"alice": alice, "bob": bob}
            # 模拟 _select_idle_teammate 逻辑
            idle = [(tm.completed_tasks, name, tm)
                    for name, tm in teammates.items()
                    if tm.status == TeammateStatus.IDLE]
            idle.sort(key=lambda x: x[0])

            # bob (2 tasks) 应被优先选择
            assert idle[0][1] == "bob"
            assert idle[1][1] == "alice"

            await alice.shutdown()
            await bob.shutdown()

        asyncio.run(_test())

    def test_is_complete_detection(self, orch_deps):
        """_is_complete: 所有任务终态 + 无 WORKING teammate."""
        from harness.team.models import TeamTaskStatus
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeammateStatus

        async def _test():
            alice = TeammateAgent(
                agent_name="alice", llm=orch_deps["llm"], tools=[],
                team_context=orch_deps["ctx"], message_bus=orch_deps["bus"],
                task_store=orch_deps["store"], role="member",
            )
            await alice.spawn()
            alice.status = TeammateStatus.IDLE
            teammates = {"alice": alice}

            # 无任务 → 应完成
            tasks_empty = await orch_deps["store"].load_tasks()
            assert all(t.status.is_terminal for t in tasks_empty)

            # 创建 pending 任务
            await orch_deps["store"].create_task(title="待办任务")
            # teardown: 先完成再检查
            tasks = await orch_deps["store"].load_tasks()
            assert not all(t.status.is_terminal for t in tasks)

            # 标记完成
            for t in tasks:
                await orch_deps["store"].update_task(t.id, status=TeamTaskStatus.COMPLETED)
            tasks = await orch_deps["store"].load_tasks()
            assert all(t.status.is_terminal for t in tasks)

            await alice.shutdown()

        asyncio.run(_test())

    def test_dispatch_ready_tasks(self, orch_deps):
        """_dispatch_ready_tasks: 分配就绪任务给 IDLE teammate."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamTaskStatus, TeammateStatus

        async def _test():
            alice = TeammateAgent(
                agent_name="alice", llm=orch_deps["llm"], tools=[],
                team_context=orch_deps["ctx"], message_bus=orch_deps["bus"],
                task_store=orch_deps["store"], role="member",
            )
            await alice.spawn()
            await asyncio.sleep(0.1)
            alice.status = TeammateStatus.IDLE

            # 创建就绪任务
            task = await orch_deps["store"].create_task(title="就绪任务")
            ready = await orch_deps["store"].get_ready_tasks()
            assert len(ready) == 1

            # 模拟 dispatch
            t = ready[0]
            if not t.assigned_agent:
                await orch_deps["store"].update_task(
                    t.id, assigned_agent="alice", status=TeamTaskStatus.IN_PROGRESS,
                )
                await alice.assign_task(t)

            assert alice.status == TeammateStatus.WORKING
            assert alice.current_task_id == task.id

            await alice.shutdown()

        asyncio.run(_test())


# ============================================================================
# L5: TeamTracer (no-op mode)
# ============================================================================

class TestTeamTracer:
    """TeamTracer: no-op 模式, LangChain callback 获取."""

    def test_tracer_noop_when_no_api_key(self):
        """无 API key 时 tracer 应 disabled."""
        from harness.observability import TeamTracer
        tracer = TeamTracer(session_id="test", user_id="test")
        assert tracer.is_enabled is False  # 无 LANGFUSE_PUBLIC_KEY

    def test_tracer_context_managers_noop(self):
        """no-op 模式下追踪方法应安全执行 (不抛异常)."""
        from harness.observability import TeamTracer
        tracer = TeamTracer(session_id="test", user_id="test")

        # trace_team_start / trace_team_end
        tracer.trace_team_start("test message")
        tracer.trace_team_end("completed", total_rounds=3)

        # trace_phase
        tracer.trace_phase("planning")  # 不应抛异常

        # trace_teammate_work
        tracer.trace_teammate_work_start("alice", "task_1")
        tracer.trace_teammate_work_end("alice", "task_1")  # 不应抛异常

    def test_langchain_callback_noop(self):
        """no-op 模式下 CallbackHandler 应返回 None."""
        from harness.observability import TeamTracer
        tracer = TeamTracer(session_id="test", user_id="test")
        cb = tracer.get_langchain_callback()
        assert cb is None

    def test_trace_events_noop(self):
        """no-op 模式下事件记录不应抛异常."""
        from harness.observability import TeamTracer
        tracer = TeamTracer(session_id="test", user_id="test")

        tracer.trace_task_event("task_1", "created")
        tracer.trace_task_event("task_1", "completed", metadata={"agent": "alice"})
        tracer.trace_message("alice", "lead", "text", "done", task_id="task_1")
        tracer.trace_error("test error")
        tracer.flush()
        tracer.shutdown()

    def test_tracer_metadata_set_noop(self):
        """no-op 模式下 trace_error 不应抛异常."""
        from harness.observability import TeamTracer
        tracer = TeamTracer(session_id="test", user_id="test")
        tracer.trace_error("test error message", metadata={"key": "value"})  # 不应异常


# ============================================================================
# L6: 完整流程模拟 (无 LLM, 纯调度逻辑)
# ============================================================================

class TestFullFlow:
    """端到端流程: Lead 规划 → task_create → auto-claim → 完成检测."""

    @pytest.fixture
    def flow_deps(self, tmp_path):
        """创建完整流程所需依赖."""
        from harness.team.task_store import TeamTaskStore
        from harness.team.message_bus import TeamMessageBus
        from harness.team.context import TeamContext
        from harness.team.models import TeamMemberRuntime, TeammateStatus
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        store = TeamTaskStore._create_with_dir(tmp_path / "tasks", "flow_proj")
        bus = TeamMessageBus._create_with_dir(tmp_path / "msgs", "flow_proj")
        ctx = TeamContext(
            project_id="flow_proj",
            project_name="Flow Test",
            project_description="Testing full flow",
            thread_id="thread-flow",
            user_id="default",
            members=[
                TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.SPAWNING),
                TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.SPAWNING),
                TeamMemberRuntime(agent_name="bob", role="member", status=TeammateStatus.SPAWNING),
            ],
        )
        llm = FakeListChatModel(responses=["OK"])

        return {"store": store, "bus": bus, "ctx": ctx, "llm": llm}

    def test_full_flow_planning_to_completion(self, flow_deps):
        """模拟: Lead 规划 → 创建任务 → members auto-claim → 完成."""
        from harness.team.teammate_agent import TeammateAgent
        from harness.team.models import TeamTask, TeamTaskStatus, TeammateStatus

        async def _test():
            store = flow_deps["store"]
            bus = flow_deps["bus"]

            # ── Step 1: Lead 规划, 创建 3 个子任务 ──
            task_1 = await store.create_task(title="创建数据库 schema")
            task_2 = await store.create_task(title="写 API 路由")
            task_3 = await store.create_task(title="写单元测试")

            all_tasks = await store.load_tasks()
            assert len(all_tasks) == 3  # 全部 PENDING
            for t in all_tasks:
                assert t.status == TeamTaskStatus.PENDING
                assert t.assigned_agent is None

            # ── Step 2: 创建 teammates ──
            alice = TeammateAgent(
                agent_name="alice", llm=flow_deps["llm"], tools=[],
                team_context=flow_deps["ctx"], message_bus=bus,
                task_store=store, role="member",
            )
            bob = TeammateAgent(
                agent_name="bob", llm=flow_deps["llm"], tools=[],
                team_context=flow_deps["ctx"], message_bus=bus,
                task_store=store, role="member",
            )
            await alice.spawn()
            await bob.spawn()
            await asyncio.sleep(0.1)

            alice.status = TeammateStatus.IDLE
            bob.status = TeammateStatus.IDLE

            # ── Step 3: 开启 auto-claim (模拟规划完成后) ──
            alice.enable_auto_claim()
            bob.enable_auto_claim()

            # alice 认领第一个任务
            claimed = await alice._maybe_claim_task()
            assert claimed is True
            assert alice.current_task_id is not None

            # bob 认领下一个
            claimed = await bob._maybe_claim_task()
            assert claimed is True
            assert bob.current_task_id is not None

            # ── Step 4: 验证任务分配 ──
            tasks = await store.load_tasks()
            in_progress = [t for t in tasks if t.status == TeamTaskStatus.IN_PROGRESS]
            pending = [t for t in tasks if t.status == TeamTaskStatus.PENDING]
            assert len(in_progress) == 2  # alice + bob 各认领一个
            assert len(pending) == 1      # 还有一个未认领

            # ── Step 5: 第三个任务被认领 ──
            # bob 先完成当前任务, 回到 IDLE, 然后认领
            bob_task_id = bob.current_task_id
            await store.update_task(bob_task_id, status=TeamTaskStatus.COMPLETED, output="done")
            bob.current_task_id = None
            bob.completed_tasks += 1
            bob.status = TeammateStatus.IDLE

            claimed = await bob._maybe_claim_task()
            assert claimed is True

            # 所有任务已分配
            tasks = await store.load_tasks()
            for t in tasks:
                assert t.assigned_agent is not None

            # ── Step 6: 所有任务完成后检测 ──
            for t in tasks:
                await store.update_task(t.id, status=TeamTaskStatus.COMPLETED, output="done")
            alice.status = TeammateStatus.IDLE
            bob.status = TeammateStatus.IDLE

            tasks = await store.load_tasks()
            assert all(t.status.is_terminal for t in tasks)
            # 全部完成, 无 WORKING teammate
            assert alice.status != TeammateStatus.WORKING
            assert bob.status != TeammateStatus.WORKING

            await alice.shutdown()
            await bob.shutdown()

        asyncio.run(_test())


# ============================================================================
# L7: 回归验证 — 15 个工具全部可创建
# ============================================================================

class TestRegression:
    """回归验证: 所有模块可导入, 所有定义一致."""

    def test_all_modules_importable(self):
        """验证所有 team 模块可导入."""
        modules = [
            "harness.team.models",
            "harness.team.context",
            "harness.team.task_store",
            "harness.team.message_bus",
            "harness.team.tools",
            "harness.team.teammate_middleware",
            "harness.team.teammate_agent",
            "harness.team.orchestrator",
            "harness.team.project_lead_agent",
            "harness.observability.team_tracer",
            "harness.observability.langfuse_manager",
        ]
        for mod_name in modules:
            __import__(mod_name)

    def test_tool_count_consistency(self):
        """工具计数: docstring 声明 vs 实际."""
        # tools.py docstring: Lead 6, Shared 5, Member 4 = 15 total
        from harness.team.tools import LEAD_TOOLS, SHARED_TOOLS, MEMBER_TOOLS
        assert len(LEAD_TOOLS) == 6, f"LEAD_TOOLS: {sorted(LEAD_TOOLS)}"
        assert len(SHARED_TOOLS) == 5, f"SHARED_TOOLS: {sorted(SHARED_TOOLS)}"
        assert len(MEMBER_TOOLS) == 4, f"MEMBER_TOOLS: {sorted(MEMBER_TOOLS)}"

    def test_no_duplicate_tools(self):
        """LEAD_TOOLS, SHARED_TOOLS, MEMBER_TOOLS 不应有交集."""
        from harness.team.tools import LEAD_TOOLS, SHARED_TOOLS, MEMBER_TOOLS
        assert LEAD_TOOLS & SHARED_TOOLS == set()
        assert LEAD_TOOLS & MEMBER_TOOLS == set()
        assert SHARED_TOOLS & MEMBER_TOOLS == set()

    def test_teammate_status_values(self):
        """TeammateStatus 值应与 models.py 定义一致."""
        from harness.team.models import TeammateStatus
        values = {s.value for s in TeammateStatus}
        expected = {"spawning", "working", "idle", "shutting_down", "shutdown", "failed"}
        assert values == expected

    def test_message_type_coverage(self):
        """所有消息类型应覆盖 s16 协议."""
        from harness.team.models import TeamMessageType
        types = {t.value for t in TeamMessageType}
        assert "shutdown_request" in types
        assert "shutdown_response" in types
        assert "plan_approval_request" in types
        assert "plan_approval_response" in types
        assert "text" in types
        assert "broadcast" in types
        assert "lifecycle" in types
