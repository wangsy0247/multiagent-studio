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

    def __init__(self, messages: list, runtime) -> None:
        self.messages = list(messages)
        self.runtime = runtime

    def override(self, **kwargs):
        return _FakeRequest(kwargs.get("messages", self.messages), self.runtime)


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
