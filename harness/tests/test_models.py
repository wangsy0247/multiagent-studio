"""Tests for data models (models.py)."""
from __future__ import annotations

import pytest
from datetime import datetime

from harness.models import (
    HarnessState,
    TodoItem,
    ClarificationRequest,
    TokenUsage,
    SubAgentConfig,
    SubAgentResult,
    AgentNode,
    ExecutionGraph,
    EvaluationCriteria,
    EvaluationResult,
    SubAgentEvaluation,
    MemorySignal,
    ValidationResult,
    ExecuteRequest,
    ClarificationResponse,
    ToolGroup,
    initial_state,
)
from langchain_core.messages import HumanMessage


class TestTodoItem:
    def test_default_values(self):
        todo = TodoItem(description="Test task")
        assert todo.status == "pending"
        assert todo.assigned_agent is None
        assert todo.id != ""

    def test_status_transition(self):
        todo = TodoItem(description="Task", status="in_progress")
        assert todo.status == "in_progress"

    def test_all_status_values(self):
        for status in ["pending", "in_progress", "completed", "failed"]:
            todo = TodoItem(description="Task", status=status)  # type: ignore[arg-type]
            assert todo.status == status


class TestClarificationRequest:
    def test_default_values(self):
        cr = ClarificationRequest(question="Are you sure?")
        assert cr.question == "Are you sure?"
        assert cr.context == ""
        assert cr.required is False
        assert cr.answer is None
        assert cr.options is None

    def test_with_answer(self):
        cr = ClarificationRequest(question="Confirm?", answer="yes", required=True)
        assert cr.answer == "yes"
        assert cr.required is True


class TestTokenUsage:
    def test_default_zero(self):
        tu = TokenUsage()
        assert tu.prompt_tokens == 0
        assert tu.completion_tokens == 0
        assert tu.total_tokens == 0
        assert tu.cost_usd == 0.0

    def test_custom_values(self):
        tu = TokenUsage(prompt_tokens=500, completion_tokens=200, total_tokens=700, cost_usd=0.03)
        assert tu.total_tokens == 700
        assert tu.cost_usd == 0.03


class TestSubAgentConfig:
    def test_minimal_config(self):
        cfg = SubAgentConfig(
            name="test",
            display_name="Test",
            system_prompt="You are a test agent.",
        )
        assert cfg.model == "inherit"
        assert cfg.tools is None
        assert cfg.temperature == 0.3
        assert cfg.max_turns == 50
        assert cfg.disallowed_tools == ["task", "ask_clarification", "present_files"]

    def test_full_config(self):
        cfg = SubAgentConfig(
            name="full",
            display_name="Full Agent",
            description="A full featured agent",
            system_prompt="You are helpful.",
            model="gpt-4o-mini",
            tools=["python", "bash"],
            temperature=0.7,
            max_turns=20,
            disallowed_tools=["task"],
        )
        assert cfg.tools == ["python", "bash"]
        assert cfg.max_turns == 20
        assert cfg.disallowed_tools == ["task"]


class TestSubAgentResult:
    def test_success(self):
        r = SubAgentResult(status="success", output="Done", iterations=3)
        assert r.status == "success"
        assert r.iterations == 3

    def test_error(self):
        r = SubAgentResult(status="error", output="Failed")
        assert r.status == "error"
        assert r.iterations == 0


class TestHarnessState:
    def test_initial_state(self, thread_id, user_id):
        state = initial_state(thread_id, user_id, "Hello")
        assert state["thread_id"] == thread_id
        assert state["user_id"] == user_id
        assert len(state["messages"]) == 1
        assert isinstance(state["messages"][0], HumanMessage)
        assert state["plan_mode"] is False

    def test_mutable_state(self, empty_state):
        empty_state["plan_mode"] = True
        empty_state["todos"] = [TodoItem(description="Task 1")]
        assert empty_state["plan_mode"] is True
        assert len(empty_state["todos"]) == 1


class TestEvaluationCriteria:
    def test_criteria(self):
        c = EvaluationCriteria(
            name="accuracy",
            display_name="Accuracy",
            description="How accurate",
            weight=0.4,
            rubric="10:perfect",
        )
        assert c.weight == 0.4
        assert c.name == "accuracy"


class TestMemorySignal:
    def test_correction_signal(self):
        s = MemorySignal(type="correction", content="That is wrong.")
        assert s.type == "correction"

    def test_affirmation_signal(self):
        s = MemorySignal(type="affirmation", content="Great!")
        assert s.type == "affirmation"


class TestExecuteRequest:
    def test_minimal_request(self):
        req = ExecuteRequest(thread_id="t1", user_id="u1", message="Hello")
        assert req.execution_graph is None


class TestToolGroup:
    def test_tool_group(self):
        tg = ToolGroup(name="search", description="Search tools", tools=["web_search"])
        assert tg.dynamic is False
