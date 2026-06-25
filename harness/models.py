"""Pydantic models and LangGraph state definitions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class TodoItem(BaseModel):
    """Plan mode TODO item."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    assigned_agent: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None


class ClarificationRequest(BaseModel):
    """Pending human clarification request."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    context: str = ""
    options: list[str] | None = None
    required: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: datetime | None = None
    answer: str | None = None


class TokenUsage(BaseModel):
    """Token consumption statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class SubAgentConfig(BaseModel):
    """SubAgent configuration — mirrors DeerFlow SubagentConfig.

    Attributes:
        name: Unique identifier.
        display_name: Human-readable name.
        description: When to delegate to this subagent.
        system_prompt: System prompt guiding the subagent's behavior.
        model: Model to use — 'inherit' uses parent's model.
        tools: Optional list of allowed tool names. None inherits all.
        disallowed_tools: Tools to deny even when tools=None.
        temperature: LLM temperature.
        max_turns: Maximum agent turns before stopping.
    """

    name: str
    display_name: str
    description: str = ""
    system_prompt: str
    model: str = "inherit"
    tools: list[str] | None = None
    disallowed_tools: list[str] = ["task", "ask_clarification", "present_files"]
    temperature: float = 0.3
    max_turns: int = 50


class AgentNode(BaseModel):
    """Agent node on the frontend canvas."""

    id: str
    type: Literal["lead", "subagent"]
    config: SubAgentConfig
    position: dict[str, float]
    connections: list[str] = []


class ExecutionGraph(BaseModel):
    """Execution graph built by the frontend."""

    nodes: list[AgentNode]
    edges: list[tuple[str, str]]
    entry_point: str


class SubAgentResult(BaseModel):
    """SubAgent execution result."""

    status: Literal["success", "error", "max_iterations_reached"]
    output: str
    iterations: int = 0


class EvaluationCriteria(BaseModel):
    """Evaluation criterion for Judge."""

    name: str
    display_name: str
    description: str
    weight: float
    rubric: str


class EvaluationResult(BaseModel):
    """Judge evaluation result."""

    scores: dict[str, dict[str, Any]] = {}
    overall_score: float = 0.0
    strengths: list[str] = []
    weaknesses: list[str] = []
    suggestions: list[str] = []
    summary: str = ""


class SubAgentEvaluation(BaseModel):
    """SubAgent result evaluation."""

    completeness: float = 0.0
    accuracy: float = 0.0
    instruction_following: float = 0.0
    overall: float = 0.0
    feedback: str = ""


class MemorySignal(BaseModel):
    """Memory signal extracted from conversation."""

    type: Literal["correction", "affirmation", "fact"]
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)


class ValidationResult(BaseModel):
    """Output validation result."""

    valid: bool
    issues: list[str] = []


class HarnessState(TypedDict, total=False):
    """LangGraph state definition — compatible with create_agent() as state_schema.

    Used for both the inner create_agent() subgraph and the outer orchestration
    graph. The ``messages`` key with ``add_messages`` reducer is required by
    create_agent(); all other fields are carried through transparently.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    thread_id: str
    user_id: str
    plan_mode: bool
    todos: list[TodoItem]
    memory_context: str
    pending_clarification: ClarificationRequest | None
    pending_task_calls: list[dict[str, Any]]  # subagent dispatch queue
    subagent_results: dict[str, Any]
    token_usage: TokenUsage
    is_finished: bool
    metadata: dict[str, Any]
    # Runtime injected fields
    workspace: str
    sandbox: Any
    agent_type: str
    loop_detected: bool
    context_lost: bool
    plan_mode_exit: bool
    suggested_title: str
    last_error: dict[str, Any]
    evaluation: EvaluationResult


def _human_message_with_files(message: str, files: list[dict] | None = None) -> HumanMessage:
    """Create a HumanMessage with optional uploaded file metadata."""
    kwargs: dict[str, Any] = {}
    if files:
        kwargs["files"] = files
    return HumanMessage(content=message, additional_kwargs=kwargs)


def initial_state(
    thread_id: str,
    user_id: str,
    message: str,
    files: list[dict] | None = None,
) -> HarnessState:
    return HarnessState(
        messages=[_human_message_with_files(message, files)],
        thread_id=thread_id,
        user_id=user_id,
        plan_mode=False,
        todos=[],
        memory_context="",
        pending_clarification=None,
        pending_task_calls=[],
        subagent_results={},
        token_usage=TokenUsage(),
        is_finished=False,
        metadata={},
    )


# Request/response models for API
class ExecuteRequest(BaseModel):
    thread_id: str
    user_id: str
    message: str
    execution_graph: ExecutionGraph | None = None
    files: list[dict] | None = None


class ClarificationResponse(BaseModel):
    clarification_id: str
    answer: str


class CreateAgentRequest(BaseModel):
    config: SubAgentConfig


class ToolGroup(BaseModel):
    """Group of tools."""

    name: str
    description: str
    tools: list[str]
    dynamic: bool = False
