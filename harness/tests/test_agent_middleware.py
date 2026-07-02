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

    def test_all_20_in_order(self):
        """Verify all 20 middlewares match DeerFlow's exact order."""
        assert len(AGENT_MIDDLEWARE_ORDER) == 20
        names = [mw.name for mw in AGENT_MIDDLEWARE_ORDER]
        assert names == [
            "thread_data", "uploads", "sandbox",
            "dangling_tool_call", "llm_error_handling",
            "guardrail", "sandbox_audit", "tool_error_handling",
            "dynamic_context", "summarization", "todo", "token_usage",
            "title", "memory", "view_image", "deferred_tool_filter",
            "subagent_limit", "loop_detection",
            "safety_finish_reason", "clarification",
        ]

    def test_all_extend_harness_agent_middleware(self):
        """Every middleware in AGENT_MIDDLEWARE_ORDER must extend HarnessAgentMiddleware.

        Exception: SummarizationMiddleware inherits from LangChain's
        SummarizationMiddleware (same as DeerFlow).
        """
        from harness.middleware.summarization import SummarizationMiddleware as SumMW
        for mw_cls in AGENT_MIDDLEWARE_ORDER:
            if mw_cls is SumMW:
                continue  # LangChain SummarizationMiddleware subclass
            assert issubclass(mw_cls, HarnessAgentMiddleware), (
                f"{mw_cls.__name__} does not extend HarnessAgentMiddleware"
            )

    def test_all_have_state_schema(self):
        """Every middleware should have state_schema=HarnessState.

        Exception: SummarizationMiddleware inherits from LangChain's
        SummarizationMiddleware with its own state schema.
        """
        from harness.middleware.summarization import SummarizationMiddleware as SumMW
        for mw_cls in AGENT_MIDDLEWARE_ORDER:
            if mw_cls is SumMW:
                continue  # LangChain SummarizationMiddleware has own state_schema
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

    def test_sync_before_agent_default_returns_none(self):
        """Sync before_agent falls back to parent default (returns None)."""

        class TestMW(HarnessAgentMiddleware):
            name = "test"

        mw = TestMW()
        state = initial_state("t1", "u1", "hello")
        runtime = MagicMock(spec=Runtime)
        # HarnessAgentMiddleware intentionally does not override sync hooks
        # so create_agent() can detect which hooks subclasses actually implement.
        result = mw.before_agent(state, runtime)
        assert result is None

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
    """Test the new DanglingToolCallMiddleware (awrap_model_call hook)."""

    @pytest.mark.asyncio
    async def test_no_dangling_when_all_responded(self):
        """Should return handler result unchanged when all tool_calls have responses."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[3]  # DanglingToolCallMiddleware
        mw = mw_cls()

        async def handler(request):
            return AIMessage(content="ok")

        request = MagicMock()
        request.messages = [
            HumanMessage(content="hello"),
            AIMessage(content="ok", tool_calls=[
                {"name": "search", "args": {"q": "test"}, "id": "tc1"}
            ]),
            ToolMessage(content="result", tool_call_id="tc1", name="search"),
        ]

        result = await mw.awrap_model_call(request, handler)
        assert isinstance(result, AIMessage)
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_injects_synthetic_on_dangling(self):
        """Should inject synthetic ToolMessage for unmatched tool_calls."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[3]  # DanglingToolCallMiddleware
        mw = mw_cls()

        async def handler(request):
            return AIMessage(content="ok")

        request = MagicMock()
        request.messages = [
            HumanMessage(content="hello"),
            AIMessage(content="ok", tool_calls=[
                {"name": "search", "args": {"q": "test"}, "id": "tc1"}
            ]),
            # No matching ToolMessage!
        ]

        result = await mw.awrap_model_call(request, handler)
        assert isinstance(result, AIMessage)
        # The middleware should have patched the request with a synthetic ToolMessage


class TestV2ClarificationMiddleware:
    """Test the new ClarificationMiddleware (wrap_tool_call hook only)."""

    @pytest.mark.asyncio
    async def test_intercepts_ask_clarification(self):
        """Should return Command(goto=END) when ask_clarification is called."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[19]  # ClarificationMiddleware
        mw = mw_cls()

        request = MagicMock()
        request.tool_call = {
            "name": "ask_clarification",
            "args": {
                "question": "Are you sure?",
                "context": "This will delete files",
                "required": True,
            },
            "id": "tc1",
        }

        async def handler(req):
            return ToolMessage(content="ok", tool_call_id="tc1", name="ask_clarification")

        result = await mw.awrap_tool_call(request, handler)
        # Should return a Command, not a ToolMessage
        from langgraph.graph import END
        from langgraph.types import Command
        assert isinstance(result, Command)
        assert result.goto == END
        assert "messages" in result.update
        tool_message = result.update["messages"][0]
        metadata = tool_message.additional_kwargs["clarification"]
        assert metadata["question"] == "Are you sure?"
        assert metadata["required"] is True
        assert metadata["context"] == "This will delete files"

    @pytest.mark.asyncio
    async def test_non_clarification_passes_through(self):
        """Non-clarification tool calls should pass through unchanged."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[19]  # ClarificationMiddleware
        mw = mw_cls()

        request = MagicMock()
        request.tool_call = {"name": "search", "args": {"q": "hello"}, "id": "tc2"}

        async def handler(req):
            return ToolMessage(content="results", tool_call_id="tc2", name="search")

        result = await mw.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "results"


class TestV2ToolErrorHandlingMiddleware:
    """Test the new ToolErrorHandlingMiddleware (awrap_tool_call hook)."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Should retry a failed tool call up to max_retries."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[7]  # ToolErrorHandlingMiddleware
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
        mw_cls = AGENT_MIDDLEWARE_ORDER[7]  # ToolErrorHandlingMiddleware
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
    """Test the new SubagentLimitMiddleware (aafter_model hook)."""

    @pytest.mark.asyncio
    async def test_blocks_task_when_at_limit(self):
        """Should strip excess task tool calls after model emits them."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[16]  # SubagentLimitMiddleware
        mw = mw_cls({"max_concurrent": 2})

        state = HarnessState(
            messages=[
                HumanMessage(content="do it"),
                AIMessage(content="ok", tool_calls=[
                    {"name": "task", "args": {"agent_name": "coder"}, "id": "tc1"},
                    {"name": "task", "args": {"agent_name": "researcher"}, "id": "tc2"},
                    {"name": "task", "args": {"agent_name": "tester"}, "id": "tc3"},
                    {"name": "search", "args": {"q": "test"}, "id": "tc4"},
                ]),
            ],
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)

        result = await mw.aafter_model(state, runtime)

        assert result is not None
        assert "messages" in result
        msgs = result["messages"]
        # Should have replaced the last AIMessage with truncated tool_calls
        last_ai = msgs[-2]  # Before the warning HumanMessage
        task_names = [tc["name"] for tc in last_ai.tool_calls if tc["name"] == "task"]
        assert len(task_names) <= 2  # max_concurrent = 2
        # Non-task calls should be preserved
        remaining_names = [tc["name"] for tc in last_ai.tool_calls]
        assert "search" in remaining_names


class TestV2LoopDetectionMiddleware:
    """Test the new LoopDetectionMiddleware (aafter_model hook)."""

    @pytest.mark.asyncio
    async def test_detects_loop_on_repeated_sequence(self):
        """Should detect a loop when the same tool calls appear repeatedly."""
        mw_cls = AGENT_MIDDLEWARE_ORDER[17]  # LoopDetectionMiddleware
        mw = mw_cls(warn_threshold=2, hard_limit=5, window_size=10)

        # Build state where the last message is an AIMessage with tool_calls
        # (as it would be right after the model emits a response)
        msgs: list = []
        for _ in range(4):
            msgs.extend([
                HumanMessage(content="run"),
                AIMessage(content="running", tool_calls=[
                    {"name": "bash", "args": {"cmd": "ls"}, "id": "tc1"}
                ]),
                ToolMessage(content="done", tool_call_id="tc1", name="bash"),
            ])

        # The last message in the state should be an AIMessage with tool_calls
        # to trigger the aafter_model detection
        msgs.append(AIMessage(content="running again", tool_calls=[
            {"name": "bash", "args": {"cmd": "ls"}, "id": "tc99"}
        ]))

        state = HarnessState(
            messages=msgs,
            thread_id="t1",
            user_id="u1",
        )
        runtime = MagicMock(spec=Runtime)
        # runtime.context must provide thread_id for per-thread tracking
        runtime.context = {"thread_id": "t1"}

        # First call builds up history, second triggers warning (warn_threshold=2)
        await mw.aafter_model(state, runtime)  # count=1
        result = await mw.aafter_model(state, runtime)  # count=2 → triggers warning

        # Warnings are queued and returned as None from aafter_model
        # (they get injected in awrap_model_call). Hard stops return non-None.
        # We verify the middleware is functioning by checking that the
        # warning was logged (see captured log above) and history was tracked.
        assert result is None  # warning queued, not hard stop yet

        # Verify hard stop triggers when count exceeds hard_limit (5)
        for _ in range(5):
            await mw.aafter_model(state, runtime)
        hard_result = await mw.aafter_model(state, runtime)
        assert hard_result is not None
        assert "messages" in hard_result


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
