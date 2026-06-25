"""Tests for the new AgentMiddleware-based system (Phase 1-5 migration)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware import AGENT_MIDDLEWARE_ORDER
from harness.models import HarnessState, ClarificationRequest, initial_state


# ---------------------------------------------------------------------------
# Phase 1: Foundation
# ---------------------------------------------------------------------------


class TestHarnessAgentMiddleware:
    """Test the HarnessAgentMiddleware base class."""

    def test_all_14_in_order(self):
        """Verify all 14 middlewares are in the correct order."""
        assert len(AGENT_MIDDLEWARE_ORDER) == 14
        names = [mw.name for mw in AGENT_MIDDLEWARE_ORDER]
        assert names == [
            "thread_data", "sandbox", "uploads", "dangling_tool_call",
            "guardrail", "tool_error_handling", "summarization", "todo",
            "title", "memory", "view_image", "subagent_limit",
            "loop_detection", "clarification",
        ]

    def test_all_extend_harness_agent_middleware(self):
        """Every middleware in AGENT_MIDDLEWARE_ORDER must extend HarnessAgentMiddleware."""
        for mw_cls in AGENT_MIDDLEWARE_ORDER:
            assert issubclass(mw_cls, HarnessAgentMiddleware), (
                f"{mw_cls.__name__} does not extend HarnessAgentMiddleware"
            )

    def test_all_have_state_schema(self):
        """Every middleware should have state_schema=HarnessState."""
        for mw_cls in AGENT_MIDDLEWARE_ORDER:
            assert mw_cls.state_schema is HarnessState, (
                f"{mw_cls.__name__}.state_schema is not HarnessState"
            )

    @pytest.mark.asyncio
    async def test_abefore_agent_default_returns_none(self):
        """Default abefore_agent returns None (continue)."""

        class TestMW(HarnessAgentMiddleware):
            name = "test"

        mw = TestMW()
        state = initial_state("t1", "u1", "hello")
        runtime = MagicMock(spec=Runtime)
        result = await mw.abefore_agent(state, runtime)
        assert result is None

    def test_sync_before_agent_raises_not_implemented(self):
        """Sync before_agent should raise NotImplementedError."""

        class TestMW(HarnessAgentMiddleware):
            name = "test"

        mw = TestMW()
        state = initial_state("t1", "u1", "hello")
        runtime = MagicMock(spec=Runtime)
        with pytest.raises(NotImplementedError):
            mw.before_agent(state, runtime)

    def test_config_storage(self):
        """Config should be stored and accessible."""

        class TestMW(HarnessAgentMiddleware):
            name = "test"

        mw = TestMW({"key": "value", "num": 42})
        assert mw.config["key"] == "value"
        assert mw.config["num"] == 42


# ---------------------------------------------------------------------------
# Phase 2: Middleware-specific tests
# ---------------------------------------------------------------------------


class TestV2ThreadDataMiddleware:
    """Test the new ThreadDataMiddleware."""

    @pytest.mark.asyncio
    async def test_creates_directories(self, tmp_path):
        """Should create the workspace/uploads/outputs directory structure."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[0]
        mw = mw_cls({"workspace_root": str(tmp_path)})

        state = initial_state("thread-1", "user-1", "hello")
        runtime = MagicMock(spec=Runtime)

        result = await mw.abefore_agent(state, runtime)

        assert result is not None
        assert "workspace" in result
        assert "thread_data" in result
        # Verify directories exist
        ws = Path(result["workspace"])
        assert ws.exists()
        uploads = ws.parent / "uploads"
        assert uploads.exists()
        outputs = ws.parent / "outputs"
        assert outputs.exists()


class TestV2DanglingToolCallMiddleware:
    """Test the new DanglingToolCallMiddleware (abefore_model hook)."""

    @pytest.mark.asyncio
    async def test_no_dangling_when_all_responded(self):
        """Should return None when all tool_calls have matching ToolMessages."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[3]
        mw = mw_cls()

        state = HarnessState(
            messages=[
                HumanMessage(content="hello"),
                AIMessage(content="ok", tool_calls=[
                    {"name": "search", "args": {"q": "test"}, "id": "tc1"}
                ]),
                ToolMessage(content="result", tool_call_id="tc1", name="search"),
            ],
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.abefore_model(state, runtime)
        assert result is None

    @pytest.mark.asyncio
    async def test_injects_synthetic_on_dangling(self):
        """Should inject synthetic ToolMessage for unmatched tool_calls."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[3]
        mw = mw_cls()

        state = HarnessState(
            messages=[
                HumanMessage(content="hello"),
                AIMessage(content="ok", tool_calls=[
                    {"name": "search", "args": {"q": "test"}, "id": "tc1"}
                ]),
                # No matching ToolMessage!
            ],
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.abefore_model(state, runtime)
        assert result is not None
        assert "messages" in result
        # Should have appended a synthetic ToolMessage
        msgs = result["messages"]
        last_msg = msgs[-1]
        assert isinstance(last_msg, ToolMessage)
        assert last_msg.tool_call_id == "tc1"
        assert last_msg.status == "error"


class TestV2ClarificationMiddleware:
    """Test the new ClarificationMiddleware."""

    @pytest.mark.asyncio
    async def test_creates_pending_on_ask_clarification(self):
        """Should set pending_clarification when ask_clarification tool is called."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[13]
        mw = mw_cls()

        state = HarnessState(
            messages=[
                HumanMessage(content="do something dangerous"),
                AIMessage(content="let me ask", tool_calls=[
                    {"name": "ask_clarification", "args": {
                        "question": "Are you sure?",
                        "context": "This will delete files",
                        "required": True
                    }, "id": "tc1"}
                ]),
                ToolMessage(content="waiting", tool_call_id="tc1", name="ask_clarification"),
            ],
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.aafter_model(state, runtime)

        assert result is not None
        assert "pending_clarification" in result
        pending = result["pending_clarification"]
        assert isinstance(pending, ClarificationRequest)
        assert pending.question == "Are you sure?"
        assert pending.required is True
        assert result["is_finished"] is False

    @pytest.mark.asyncio
    async def test_deduplicates_identical_questions(self):
        """Same question should not create duplicate pending requests."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[13]
        mw = mw_cls()

        messages = [
            HumanMessage(content="do it"),
            AIMessage(content="let me ask", tool_calls=[
                {"name": "ask_clarification", "args": {
                    "question": "Are you sure?", "context": "", "required": True
                }, "id": "tc1"}
            ]),
            ToolMessage(content="waiting", tool_call_id="tc1", name="ask_clarification"),
        ]

        state1 = HarnessState(messages=list(messages), thread_id="t1", user_id="u1")
        runtime = MagicMock(spec=Runtime)

        # First call should create a pending request
        result1 = await mw.aafter_model(state1, runtime)
        assert result1 is not None
        assert "pending_clarification" in result1

        # Second call with same question should be deduplicated
        state2 = HarnessState(messages=list(messages), thread_id="t1", user_id="u1")
        result2 = await mw.aafter_model(state2, runtime)
        assert result2 is None  # Deduplicated

    @pytest.mark.asyncio
    async def test_injects_answer_on_resume(self):
        """Should inject the human answer when pending_clarification has an answer."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[13]
        mw = mw_cls()

        pending = ClarificationRequest(
            question="Continue?",
            answer="yes, proceed",
            resolved_at=None,  # Will be set to detect answered state
        )
        # Override to make it "answered"
        pending.answer = "yes, proceed"

        state = HarnessState(
            messages=[HumanMessage(content="hello")],
            thread_id="t1",
            user_id="u1",
            pending_clarification=pending,
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.abefore_agent(state, runtime)
        assert result is not None
        assert result["pending_clarification"] is None
        # Should have injected the answer
        msgs = result["messages"]
        assert any(
            isinstance(m, HumanMessage) and "yes, proceed" in str(m.content)
            for m in msgs
        )


class TestV2ToolErrorHandlingMiddleware:
    """Test the new ToolErrorHandlingMiddleware (awrap_tool_call hook)."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Should retry a failed tool call up to max_retries."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[5]
        mw = mw_cls({"max_retries": 2})

        call_count = [0]

        async def failing_handler(request):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError("transient failure")
            return ToolMessage(
                content="success on retry",
                tool_call_id=request.tool_call.get("id", "tc1"),
                name=request.tool_call.get("name", "test"),
            )

        request = MagicMock()
        request.tool_call = {"name": "test", "args": {}, "id": "tc1"}

        result = await mw.awrap_tool_call(request, failing_handler)

        assert call_count[0] == 3  # 2 failures + 1 success
        assert result.content == "success on retry"

    @pytest.mark.asyncio
    async def test_returns_error_after_all_retries_exhausted(self):
        """Should return an error ToolMessage after max_retries exhausted."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[5]
        mw = mw_cls({"max_retries": 1})

        async def always_failing_handler(request):
            raise RuntimeError("permanent failure")

        request = MagicMock()
        request.tool_call = {"name": "test", "args": {}, "id": "tc1"}

        result = await mw.awrap_tool_call(request, always_failing_handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "permanent failure" in result.content


class TestV2SubagentLimitMiddleware:
    """Test the new SubagentLimitMiddleware."""

    @pytest.mark.asyncio
    async def test_blocks_task_when_at_limit(self):
        """Should strip task tool calls when at the concurrent limit."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[11]
        mw = mw_cls({"max_concurrent": 2})
        mw._active["t1"] = 2  # Already at limit

        state = HarnessState(
            messages=[
                HumanMessage(content="do it"),
                AIMessage(content="ok", tool_calls=[
                    {"name": "task", "args": {"agent_name": "coder"}, "id": "tc1"},
                    {"name": "task", "args": {"agent_name": "researcher"}, "id": "tc2"},
                    {"name": "search", "args": {"q": "test"}, "id": "tc3"},
                ]),
            ],
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.abefore_model(state, runtime)

        assert result is not None
        assert "messages" in result
        # task calls should be stripped
        msgs = result["messages"]
        last_ai = msgs[-2]  # Before the warning message
        remaining_calls = [tc["name"] for tc in last_ai.tool_calls]
        assert "task" not in remaining_calls
        assert "search" in remaining_calls


class TestV2LoopDetectionMiddleware:
    """Test the new LoopDetectionMiddleware."""

    @pytest.mark.asyncio
    async def test_detects_loop_on_repeated_sequence(self):
        """Should detect a loop when the same sequence appears repeatedly."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[12]
        mw = mw_cls({"window_size": 3, "threshold": 2})

        # Create a repeated sequence pattern
        base_msgs = [
            HumanMessage(content="run"),
            AIMessage(content="running"),
            ToolMessage(content="done", tool_call_id="tc1", name="bash"),
        ]

        state = HarnessState(
            messages=list(base_msgs) * 4,  # Repeat 4 times
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        # First calls build up history, last one should detect loop
        results = []
        for _ in range(3):
            results.append(await mw.abefore_model(state, runtime))

        # The last result should indicate a loop
        assert results[-1] is not None
        assert results[-1].get("loop_detected") is True


# ---------------------------------------------------------------------------
# Phase 3: Graph Factory
# ---------------------------------------------------------------------------


class TestGraphFactory:
    """Test the HarnessGraphFactory."""

    def _fake_llm(self):
        """Create a fake LLM that doesn't require API credentials."""
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        return GenericFakeChatModel(
            messages=iter([AIMessage(content="Hello, I am a test assistant.")])
        )

    def test_can_instantiate(self):
        """Should be able to create a factory instance."""
        from harness.graph_factory import HarnessGraphFactory

        llm = self._fake_llm()
        factory = HarnessGraphFactory(
            llm=llm,
            tools=[],
            middlewares=[],
            system_prompt="You are a helpful assistant.",
        )
        assert factory is not None

    def test_build_returns_compiled_graph(self):
        """build() should return a compiled LangGraph graph."""
        from harness.graph_factory import HarnessGraphFactory

        llm = self._fake_llm()
        factory = HarnessGraphFactory(
            llm=llm,
            tools=[],
            middlewares=[],
            system_prompt="You are a helpful assistant.",
        )

        graph = factory.build()
        # Should be a compiled graph
        assert hasattr(graph, "ainvoke")
        assert hasattr(graph, "astream")

    @pytest.mark.asyncio
    async def test_graph_invoke_works(self):
        """Should be able to invoke the compiled graph with a simple state."""
        from harness.graph_factory import HarnessGraphFactory
        from harness.models import initial_state

        llm = self._fake_llm()
        factory = HarnessGraphFactory(
            llm=llm,
            tools=[],
            middlewares=[],
            system_prompt="You are a helpful assistant.",
        )

        graph = factory.build()
        state = initial_state("t1", "u1", "Hello")

        result = await graph.ainvoke(state)
        assert result is not None
        assert "messages" in result
        assert len(result["messages"]) > 1  # At minimum: Human + AI response
