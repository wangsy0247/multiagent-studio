"""Tests for Phase 1 Lead 协调者化: 工具白名单 / coordinator prompt / 防漂移复述."""

import pytest

from harness.team.orchestrator import LEAD_ALLOWED_TOOL_GROUPS
from harness.team.teammate_agent import (
    LEAD_COORDINATOR_REMINDER,
    LEAD_REMINDER_INTERVAL,
    TeammateAgent,
)


# ===================================================================
# 工具白名单 — Lead 不授予执行类工具组
# ===================================================================


class TestLeadToolWhitelist:
    def test_files_group_excluded(self):
        """files 组含写入能力, 属于执行类, 不得授予 Lead."""
        assert "files" not in LEAD_ALLOWED_TOOL_GROUPS

    def test_readonly_groups_kept(self):
        """只读文件与搜索保留, 供 triage/synthesis 阶段了解上下文."""
        assert "files_readonly" in LEAD_ALLOWED_TOOL_GROUPS
        assert "search" in LEAD_ALLOWED_TOOL_GROUPS

    def test_execution_groups_excluded(self):
        """sandbox/code 等执行类工具组一律不在白名单."""
        for group in ("sandbox", "code", "files"):
            assert group not in LEAD_ALLOWED_TOOL_GROUPS

    def test_filter_drops_files_group_tools(self):
        """按白名单过滤后, files 组工具不出现在 Lead 工具集中."""
        tool_groups = ["files", "files_readonly", "sandbox", "search"]
        allowed = [g for g in tool_groups if g in LEAD_ALLOWED_TOOL_GROUPS]
        assert allowed == ["files_readonly", "search"]


# ===================================================================
# Lead 指令 — coordinator_workflow 段
# ===================================================================


@pytest.fixture(autouse=True)
def temp_data_root(tmp_path, monkeypatch):
    """隔离数据目录, 避免污染真实用户数据."""
    monkeypatch.setenv("HARNESS_DATA_ROOT", str(tmp_path))
    from harness.config.paths import Paths, set_paths

    set_paths(Paths())
    yield tmp_path
    set_paths(Paths())


def _make_agent(tmp_path, name: str, role: str) -> TeammateAgent:
    """创建最小可用的 TeammateAgent (不 spawn)."""
    from harness.team.task_store import TeamTaskStore
    from harness.team.message_bus import TeamMessageBus
    from harness.team.context import TeamContext
    from harness.team.models import TeamMemberRuntime, TeammateStatus
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    store = TeamTaskStore("test_proj", user_id="default", thread_id="t1")
    bus = TeamMessageBus("test_proj", user_id="default", thread_id="t1")
    ctx = TeamContext(
        project_id="test_proj",
        project_name="Test Project",
        project_description="A test project",
        user_id="default",
        members=[
            TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.IDLE),
            TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.IDLE),
        ],
    )
    return TeammateAgent(
        agent_name=name,
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        team_context=ctx,
        message_bus=bus,
        task_store=store,
        role=role,
        thread_id="t1",
    )


@pytest.fixture
def lead_agent(tmp_path):
    return _make_agent(tmp_path, "lead", "lead")


@pytest.fixture
def member_agent(tmp_path):
    return _make_agent(tmp_path, "alice", "member")


class TestLeadInstructions:
    def test_contains_coordinator_workflow_section(self, lead_agent):
        instructions = lead_agent._get_lead_instructions()
        assert "<coordinator_workflow>" in instructions
        assert "</coordinator_workflow>" in instructions

    def test_covers_four_phase_workflow(self, lead_agent):
        instructions = lead_agent._get_lead_instructions()
        for keyword in ("调研", "综合", "委派执行", "验收"):
            assert keyword in instructions

    def test_covers_self_contained_delegation(self, lead_agent):
        instructions = lead_agent._get_lead_instructions()
        assert "自包含" in instructions
        assert "看不到你的对话历史" in instructions

    def test_covers_parallel_and_verifier_separation(self, lead_agent):
        instructions = lead_agent._get_lead_instructions()
        assert "并行" in instructions
        assert "唯一验收人" in instructions

    def test_member_instructions_unaffected(self, member_agent):
        """coordinator_workflow 只出现在 Lead 指令, Member 指令不含."""
        assert "<coordinator_workflow>" not in member_agent._get_member_instructions()


# ===================================================================
# 防漂移周期性复述 — CoordinatorReminderMiddleware
# ===================================================================


def _find_reminder_middleware(agent):
    return next(
        (m for m in agent._middlewares
         if type(m).__name__ == "CoordinatorReminderMiddleware"),
        None,
    )


class _FakeRequest:
    """最小 ModelRequest 替身 — 支持 .messages 与 .override()."""

    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **kwargs):
        return _FakeRequest(kwargs.get("messages", self.messages))


class TestCoordinatorReminderMiddleware:
    def test_registered_for_lead_only(self, lead_agent, member_agent):
        assert _find_reminder_middleware(lead_agent) is not None
        assert _find_reminder_middleware(member_agent) is None

    def test_reminder_injected_on_nth_call(self, lead_agent):
        """第 N 轮 (默认第 5 轮) LLM 调用前注入复述提醒, 其余轮次不注入."""
        from langchain_core.messages import HumanMessage

        mw = _find_reminder_middleware(lead_agent)
        assert mw is not None

        seen: list[list] = []

        async def handler(request):
            seen.append(request.messages)
            return "response"

        async def _run():
            for _ in range(LEAD_REMINDER_INTERVAL):
                await mw.awrap_model_call(_FakeRequest([HumanMessage(content="hi")]), handler)

        import asyncio
        asyncio.run(_run())

        assert len(seen) == LEAD_REMINDER_INTERVAL
        # 前 N-1 轮: 消息原样透传, 无提醒
        for msgs in seen[:-1]:
            assert len(msgs) == 1
            assert all(LEAD_COORDINATOR_REMINDER not in str(m.content) for m in msgs)
        # 第 N 轮: 追加一条复述提醒
        last = seen[-1]
        assert len(last) == 2
        assert LEAD_COORDINATOR_REMINDER in str(last[-1].content)

    def test_reminder_repeats_every_interval(self, lead_agent):
        """提醒按间隔周期性触发 (第 5、10 轮都注入)."""
        from langchain_core.messages import HumanMessage

        mw = _find_reminder_middleware(lead_agent)
        injected = []

        async def handler(request):
            injected.append(
                any(LEAD_COORDINATOR_REMINDER in str(m.content) for m in request.messages)
            )
            return "response"

        async def _run():
            for _ in range(LEAD_REMINDER_INTERVAL * 2):
                await mw.awrap_model_call(_FakeRequest([HumanMessage(content="hi")]), handler)

        import asyncio
        asyncio.run(_run())

        expected = [False] * (LEAD_REMINDER_INTERVAL - 1) + [True]
        assert injected == expected * 2
