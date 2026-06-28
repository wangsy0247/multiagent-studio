"""Pydantic models and LangGraph state definitions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict

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


# ---------------------------------------------------------------------------
# Custom LangGraph reducers for HarnessState fields
# ---------------------------------------------------------------------------


def _todo_id(t: TodoItem | dict[str, Any]) -> str:
    if isinstance(t, TodoItem):
        return str(t.id)
    return str(t.get("id", ""))


def merge_todos(left: list[TodoItem], right: list[TodoItem]) -> list[TodoItem]:
    """Merge todo lists by id; later updates override earlier ones."""
    if not right:
        return left
    merged: dict[str, TodoItem] = {}
    for t in left:
        tid = _todo_id(t)
        if tid:
            merged[tid] = t
    for t in right:
        tid = _todo_id(t)
        if tid:
            merged[tid] = t
    return list(merged.values())


def merge_subagent_results(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge subagent result dictionaries."""
    if not right:
        return left
    return {**left, **right}


def merge_metadata(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge metadata updates."""
    if not right:
        return left
    return {**left, **right}


def add_token_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Add token usage counters.

    Store token usage as plain dict to avoid serializing custom Pydantic
    models into LangGraph checkpoints (matches DeerFlow approach).
    """
    return {
        "prompt_tokens": left.get("prompt_tokens", 0) + right.get("prompt_tokens", 0),
        "completion_tokens": left.get("completion_tokens", 0) + right.get("completion_tokens", 0),
        "total_tokens": left.get("total_tokens", 0) + right.get("total_tokens", 0),
        "cost_usd": left.get("cost_usd", 0.0) + right.get("cost_usd", 0.0),
    }


def append_pending_task_calls(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append new pending task calls."""
    if not right:
        return left
    return list(left) + list(right)


def append_loop_history(left: list[str], right: list[str]) -> list[str]:
    """Append loop-detection sequence hashes and cap the history length."""
    if not right:
        return left
    combined = list(left) + list(right)
    # Cap at a generous number to prevent unbounded growth while still
    # giving loop detection enough context across restarts.
    return combined[-200:]


def merge_artifacts(left: list[str], right: list[str]) -> list[str]:
    """Merge artifact path lists and deduplicate while preserving order."""
    if not right:
        return left
    combined = list(left) + [a for a in right if isinstance(a, str)]
    return list(dict.fromkeys(combined))


class HarnessState(TypedDict):
    """LangGraph state definition — compatible with create_agent() as state_schema.

    Used for both the inner create_agent() subgraph and the outer orchestration
    graph.

    Core fields (``messages``, ``thread_id``, ``user_id``) are required.
    All extension fields are marked ``NotRequired`` so middlewares and nodes can
    safely omit them until they are first produced, while still giving the type
    checker enough information to distinguish mandatory keys from optional ones.
    """

    # ------------------------------------------------------------------
    # Core required fields
    # ------------------------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]
    thread_id: str
    user_id: str

    # ------------------------------------------------------------------
    # Optional business state
    # ------------------------------------------------------------------
    plan_mode: NotRequired[bool]
    todos: NotRequired[Annotated[list[TodoItem], merge_todos]]
    memory_context: NotRequired[str]
    pending_clarification: NotRequired[ClarificationRequest | None]
    pending_task_calls: NotRequired[Annotated[list[dict[str, Any]], append_pending_task_calls]]
    subagent_results: NotRequired[Annotated[dict[str, Any], merge_subagent_results]]
    token_usage: NotRequired[Annotated[dict[str, Any], add_token_usage]]
    is_finished: NotRequired[bool]
    metadata: NotRequired[Annotated[dict[str, Any], merge_metadata]]

    # ------------------------------------------------------------------
    # Runtime injected fields
    # ------------------------------------------------------------------
    workspace: NotRequired[str]
    sandbox: NotRequired[Any]
    agent_type: NotRequired[str]
    loop_detected: NotRequired[bool]
    loop_history: NotRequired[Annotated[list[str], append_loop_history]]
    context_lost: NotRequired[bool]
    artifacts: NotRequired[Annotated[list[str], merge_artifacts]]
    plan_mode_exit: NotRequired[bool]
    suggested_title: NotRequired[str]
    title_generated: NotRequired[bool]
    last_error: NotRequired[dict[str, Any]]
    evaluation: NotRequired[EvaluationResult]


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
        token_usage={},
        is_finished=False,
        metadata={},
        title_generated=False,
        loop_history=[],
        artifacts=[],
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
