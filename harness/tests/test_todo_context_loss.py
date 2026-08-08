"""Tests for TodoMiddleware context-loss detection and premature-exit prevention."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from harness.middleware.todo import TodoMiddleware
from harness.models import initial_state, TodoItem


def _runtime(thread_id: str = "t1", run_id: str = "r1") -> SimpleNamespace:
    return SimpleNamespace(context={"thread_id": thread_id, "run_id": run_id})


def _state_with_todos(statuses: list[str]) -> dict:
    state = initial_state("t1", "u1", "hello")
    state["plan_mode"] = True
    state["todos"] = [
        TodoItem(description=f"task {i}", status=s) for i, s in enumerate(statuses)
    ]
    state["messages"] = [HumanMessage(content="hello"), AIMessage(content="working on it")]
    return state


class _FakeRequest:
    """Minimal stand-in for ModelRequest used by awrap_model_call."""

    def __init__(self, messages: list, runtime, state: dict | None = None) -> None:
        self.messages = list(messages)
        self.runtime = runtime
        self.state = state or {}

    def override(self, **kwargs):
        return _FakeRequest(kwargs.get("messages", self.messages), self.runtime, self.state)


class TestContextLossReminder:
    @pytest.mark.asyncio
    async def test_inject_reminder_when_write_todos_gone(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress", "pending"])

        result = await mw.abefore_model(state, _runtime())

        assert result is not None
        reminder = result["messages"][0]
        assert reminder.name == "todo_reminder"
        assert reminder.additional_kwargs.get("hide_from_ui") is True
        assert "task 0" in reminder.content
        assert "[in_progress]" in reminder.content

    @pytest.mark.asyncio
    async def test_no_reminder_when_write_todos_present(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[{"id": "x", "name": "write_todos", "args": {"todos": []}}],
            )
        )

        assert await mw.abefore_model(state, _runtime()) is None

    @pytest.mark.asyncio
    async def test_no_double_reminder(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["pending"])
        state["messages"].append(HumanMessage(content="old", name="todo_reminder"))

        assert await mw.abefore_model(state, _runtime()) is None

    @pytest.mark.asyncio
    async def test_no_reminder_when_no_todos(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos([])
        state["todos"] = []

        assert await mw.abefore_model(state, _runtime()) is None


class TestPrematureExitInterception:
    @pytest.mark.asyncio
    async def test_block_exit_when_incomplete(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress", "pending"])

        result = await mw.aafter_model(state, _runtime())

        assert result == {"jump_to": "model"}
        queued = mw._pending_completion_reminders[("t1", "r1")]
        assert len(queued) == 1
        assert "task 0" in queued[0]

    @pytest.mark.asyncio
    async def test_allow_exit_when_all_terminal(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["completed", "failed"])

        result = await mw.aafter_model(state, _runtime())

        assert result == {"plan_mode_exit": True}
        assert mw._pending_completion_reminders == {}

    @pytest.mark.asyncio
    async def test_no_intercept_when_tool_calls(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[{"id": "x", "name": "bash", "args": {"command": "ls"}}],
            )
        )

        assert await mw.aafter_model(state, _runtime()) is None

    @pytest.mark.asyncio
    async def test_no_intercept_when_no_todos(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos([])
        state["todos"] = []

        assert await mw.aafter_model(state, _runtime()) is None

    @pytest.mark.asyncio
    async def test_reminder_cap_prevents_infinite_loop(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        runtime = _runtime()

        first = await mw.aafter_model(state, runtime)
        second = await mw.aafter_model(state, runtime)
        third = await mw.aafter_model(state, runtime)

        assert first == {"jump_to": "model"}
        assert second == {"jump_to": "model"}
        assert third is None  # cap reached → allow exit

    @pytest.mark.asyncio
    async def test_finish_reason_tool_calls_not_clean_exit(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["pending"])
        state["messages"].append(
            AIMessage(content="", response_metadata={"finish_reason": "tool_calls"})
        )

        assert await mw.aafter_model(state, _runtime()) is None


class TestReminderInjection:
    @pytest.mark.asyncio
    async def test_wrap_injects_pending_reminder(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        runtime = _runtime()
        await mw.aafter_model(state, runtime)  # queues one reminder

        seen: dict[str, _FakeRequest] = {}

        async def handler(request):
            seen["request"] = request
            return "response"

        request = _FakeRequest([HumanMessage(content="hi")], runtime)
        result = await mw.awrap_model_call(request, handler)

        assert result == "response"
        injected = seen["request"].messages[-1]
        assert isinstance(injected, HumanMessage)
        assert injected.name == "todo_completion_reminder"
        assert injected.additional_kwargs.get("hide_from_ui") is True
        assert "task 0" in injected.content
        # Queue drained — a second wrap injects nothing new.
        await mw.awrap_model_call(request, handler)
        assert len(seen["request"].messages) == 1

    @pytest.mark.asyncio
    async def test_wrap_noop_when_nothing_pending(self) -> None:
        mw = TodoMiddleware()

        async def handler(request):
            return request

        request = _FakeRequest([HumanMessage(content="hi")], _runtime())
        result = await mw.awrap_model_call(request, handler)

        assert len(result.messages) == 1


class TestPerRunBookkeeping:
    @pytest.mark.asyncio
    async def test_before_agent_clears_other_runs(self) -> None:
        mw = TodoMiddleware()
        old_state = _state_with_todos(["in_progress"])
        await mw.aafter_model(old_state, _runtime(run_id="r-old"))
        assert mw._completion_reminder_counts.get(("t1", "r-old")) == 1

        result = await mw.abefore_agent(old_state, _runtime(run_id="r-new"))

        assert ("t1", "r-old") not in mw._completion_reminder_counts
        # plan_mode + todos non-empty → no context_lost
        assert result is None

    @pytest.mark.asyncio
    async def test_before_agent_marks_context_lost(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos([])
        state["todos"] = []

        result = await mw.abefore_agent(state, _runtime())

        assert result == {"context_lost": True}

    @pytest.mark.asyncio
    async def test_after_agent_clears_current_run(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        runtime = _runtime()
        await mw.aafter_model(state, runtime)
        assert mw._completion_reminder_counts.get(("t1", "r1")) == 1

        await mw.aafter_agent(state, runtime)

        assert ("t1", "r1") not in mw._completion_reminder_counts
        assert ("t1", "r1") not in mw._pending_completion_reminders

    @pytest.mark.asyncio
    async def test_runs_are_isolated(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])

        await mw.aafter_model(state, _runtime(run_id="r1"))
        await mw.aafter_model(state, _runtime(run_id="r1"))
        # A different run has its own budget — not affected by r1's cap.
        result = await mw.aafter_model(state, _runtime(run_id="r2"))

        assert result == {"jump_to": "model"}
        assert mw._completion_reminder_counts[("t1", "r1")] == 2
        assert mw._completion_reminder_counts[("t1", "r2")] == 1


class TestPlanModeKickoff:
    """awrap_model_call 的 plan 模式首步提示注入."""

    @staticmethod
    def _plan_state(todos: list | None = None) -> dict:
        state = initial_state("t1", "u1", "hello")
        state["plan_mode"] = True
        state["todos"] = todos or []
        state["messages"] = [HumanMessage(content="hello")]
        return state

    @pytest.mark.asyncio
    async def test_injects_kickoff_when_plan_mode_and_no_todos(self) -> None:
        mw = TodoMiddleware()
        state = self._plan_state()
        seen: dict[str, _FakeRequest] = {}

        async def handler(request):
            seen["request"] = request
            return "response"

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        await mw.awrap_model_call(request, handler)

        injected = seen["request"].messages[-1]
        assert isinstance(injected, HumanMessage)
        assert injected.name == "todo_plan_reminder"
        assert injected.additional_kwargs.get("hide_from_ui") is True
        assert "write_todos" in injected.content

    @pytest.mark.asyncio
    async def test_no_kickoff_when_plan_mode_off(self) -> None:
        mw = TodoMiddleware()
        state = self._plan_state()
        state["plan_mode"] = False

        async def handler(request):
            return request

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        result = await mw.awrap_model_call(request, handler)

        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_no_kickoff_when_todos_exist(self) -> None:
        mw = TodoMiddleware()
        state = self._plan_state([TodoItem(description="task 0", status="in_progress")])

        async def handler(request):
            return request

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        result = await mw.awrap_model_call(request, handler)

        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_no_kickoff_when_write_todos_already_called(self) -> None:
        mw = TodoMiddleware()
        state = self._plan_state()
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[{"id": "x", "name": "write_todos", "args": {"todos": []}}],
            )
        )

        async def handler(request):
            return request

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        result = await mw.awrap_model_call(request, handler)

        assert len(result.messages) == 2


class TestKeyResolutionFallback:
    """S1 回归: runtime.context 为 None (生产实况) 时 queue/drain 键必须一致."""

    @staticmethod
    def _contextless_runtime() -> SimpleNamespace:
        # 模拟生产: create_agent 的 runtime.context 为 None;
        # get_config() 在 graph 外抛 RuntimeError → 两段解析都落到 "default"
        return SimpleNamespace(context=None)

    @pytest.mark.asyncio
    async def test_queued_reminder_actually_drained(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        runtime = self._contextless_runtime()

        assert await mw.aafter_model(state, runtime) == {"jump_to": "model"}

        seen: dict[str, _FakeRequest] = {}

        async def handler(request):
            seen["request"] = request
            return "response"

        request = _FakeRequest([HumanMessage(content="hi")], runtime)
        await mw.awrap_model_call(request, handler)

        injected = seen["request"].messages[-1]
        assert injected.name == "todo_completion_reminder"
        assert "task 0" in injected.content

    @pytest.mark.asyncio
    async def test_cap_is_per_run_not_leaked_across_turns(self) -> None:
        """cap 按 run_id 隔离: abefore_agent 清掉旧 run 的计数, 新一轮有全新额度."""
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])

        # 旧 run 用满 2 次额度
        await mw.aafter_model(state, _runtime(run_id="r-old"))
        await mw.aafter_model(state, _runtime(run_id="r-old"))
        assert await mw.aafter_model(state, _runtime(run_id="r-old")) is None

        # 新一轮 (abefore_agent 先清旧 run 记账)
        await mw.abefore_agent(state, _runtime(run_id="r-new"))
        assert await mw.aafter_model(state, _runtime(run_id="r-new")) == {"jump_to": "model"}


class TestPlanModeGating:
    """S2: 非 plan 模式下 todos 相关 hook 全惰性."""

    @pytest.mark.asyncio
    async def test_after_model_inert_when_plan_mode_off(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress", "pending"])
        state["plan_mode"] = False

        assert await mw.aafter_model(state, _runtime()) is None
        # 不应产生任何排队提醒
        assert mw._pending_completion_reminders == {}

    @pytest.mark.asyncio
    async def test_before_model_inert_when_plan_mode_off(self) -> None:
        mw = TodoMiddleware()
        state = _state_with_todos(["in_progress"])
        state["plan_mode"] = False

        assert await mw.abefore_model(state, _runtime()) is None


class TestTerminalTodosReplan:
    """S3: 全终态 todos (上一轮遗留) 应重新触发规划提示."""

    @pytest.mark.asyncio
    async def test_kickoff_when_all_todos_terminal(self) -> None:
        mw = TodoMiddleware()
        state = initial_state("t1", "u1", "hello")
        state["plan_mode"] = True
        state["todos"] = [
            TodoItem(description="old task", status="completed"),
            TodoItem(description="old task 2", status="failed"),
        ]
        state["messages"] = [HumanMessage(content="new question")]
        seen: dict[str, _FakeRequest] = {}

        async def handler(request):
            seen["request"] = request
            return "response"

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        await mw.awrap_model_call(request, handler)

        assert seen["request"].messages[-1].name == "todo_plan_reminder"

    @pytest.mark.asyncio
    async def test_no_kickoff_mid_run_with_active_todos(self) -> None:
        mw = TodoMiddleware()
        state = initial_state("t1", "u1", "hello")
        state["plan_mode"] = True
        state["todos"] = [TodoItem(description="active", status="in_progress")]
        state["messages"] = [HumanMessage(content="hello")]

        async def handler(request):
            return request

        request = _FakeRequest(list(state["messages"]), _runtime(), state)
        result = await mw.awrap_model_call(request, handler)

        assert len(result.messages) == 1
