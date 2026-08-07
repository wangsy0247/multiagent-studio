"""P1-F5 修复回归: plan_approval 误判协议违规 + 工作异常有界重试."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from harness.team.context import TeamContext
from harness.team.message_bus import TeamMessageBus
from harness.team.models import (
    RequestStatus,
    TeamMemberRuntime,
    TeammateStatus,
    TeamTaskStatus,
)
from harness.team.task_store import TeamTaskStore
from harness.team.teammate_agent import TeammateAgent


class _FakeGraph:
    """astream_events 可控的空 graph (无 LLM 调用)."""

    def __init__(self, error: Exception | None = None):
        self._error = error

    def astream_events(self, input_state, config, version="v2"):
        async def _gen():
            if self._error is not None:
                raise self._error
            return
            yield  # pragma: no cover

        return _gen()


def _make_agent(project: str) -> TeammateAgent:
    store = TeamTaskStore(project, user_id="default", thread_id="t1")
    bus = TeamMessageBus(project, user_id="default", thread_id="t1")
    ctx = TeamContext(
        project_id=project,
        project_name="P",
        project_description="d",
        user_id="default",
        members=[
            TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.IDLE),
            TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.IDLE),
        ],
    )
    return TeammateAgent(
        agent_name="alice",
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        team_context=ctx,
        message_bus=bus,
        task_store=store,
        role="member",
        thread_id="t1",
    )


async def _setup_task(agent: TeammateAgent, *, retry_count: int = 0):
    task = await agent._task_store.create_task(
        title="t", assigned_agent="alice",
    )
    await agent._task_store.update_task(
        task.id, status=TeamTaskStatus.IN_PROGRESS, retry_count=retry_count,
    )
    agent.current_task_id = task.id
    return task


async def _run_work_loop(agent: TeammateAgent, error: Exception | None = None):
    with patch("langchain.agents.create_agent", return_value=_FakeGraph(error)):
        await agent._work_loop()


@pytest.mark.asyncio
async def test_plan_approval_pending_is_not_protocol_violation(tmp_path):
    """F5a: 等待 Lead 审批时任务保持 IN_PROGRESS, 不被误判 FAILED."""
    agent = _make_agent(f"proj-f5a-{tmp_path.name}")
    task = await _setup_task(agent)
    agent._pending_requests["req-1"] = {
        "type": "plan_approval",
        "status": RequestStatus.PENDING,
        "plan": "do risky thing",
    }

    await _run_work_loop(agent)

    reloaded = await agent._task_store.get_task(task.id)
    assert reloaded.status == TeamTaskStatus.IN_PROGRESS
    assert agent.failed_tasks == 0


@pytest.mark.asyncio
async def test_no_report_is_protocol_violation(tmp_path):
    """F5a 对照: 无待审批且未上报 → FAILED."""
    agent = _make_agent(f"proj-f5b-{tmp_path.name}")
    task = await _setup_task(agent)

    await _run_work_loop(agent)

    reloaded = await agent._task_store.get_task(task.id)
    assert reloaded.status == TeamTaskStatus.FAILED


@pytest.mark.asyncio
async def test_work_exception_requeues_with_retry(tmp_path):
    """F5b: 工作异常未达重试上限 → 回池 PENDING, retry_count+1."""
    agent = _make_agent(f"proj-f5c-{tmp_path.name}")
    task = await _setup_task(agent)

    await _run_work_loop(agent, error=RuntimeError("LLM rate limit"))

    reloaded = await agent._task_store.get_task(task.id)
    assert reloaded.status == TeamTaskStatus.PENDING
    assert reloaded.retry_count == 1
    assert reloaded.assigned_agent is None


@pytest.mark.asyncio
async def test_work_exception_exhausted_retries_fails(tmp_path):
    """F5b: 重试耗尽 → FAILED 终态."""
    agent = _make_agent(f"proj-f5d-{tmp_path.name}")
    task = await _setup_task(agent, retry_count=2)  # max_retries 默认 3

    await _run_work_loop(agent, error=RuntimeError("boom"))

    reloaded = await agent._task_store.get_task(task.id)
    assert reloaded.status == TeamTaskStatus.FAILED
