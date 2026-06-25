"""Shared test fixtures."""
from __future__ import annotations

import pytest

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from harness.models import (
    HarnessState,
    initial_state,
    SubAgentConfig,
    SubAgentResult,
    TokenUsage,
    ClarificationRequest,
    TodoItem,
)
from harness.middleware.base import HarnessAgentMiddleware
from harness.memory.storage import MemoryStorage, FileMemoryStorage


@pytest.fixture
def thread_id() -> str:
    return "test-thread-001"


@pytest.fixture
def user_id() -> str:
    return "test-user-001"


@pytest.fixture
def run_config() -> RunnableConfig:
    return RunnableConfig(configurable={"thread_id": "test-thread-001"})


@pytest.fixture
def empty_state(thread_id, user_id) -> HarnessState:
    return initial_state(thread_id, user_id, "Hello, world!")


@pytest.fixture
def state_with_messages(thread_id, user_id) -> HarnessState:
    state = initial_state(thread_id, user_id, "帮我计算Si的性质")
    state["messages"] = [
        HumanMessage(content="帮我计算Si的性质"),
        AIMessage(content="我将创建 SubAgent 来处理这个任务"),
    ]
    state["workspace"] = "/tmp/test-workspace"
    return state


@pytest.fixture
def subagent_config() -> SubAgentConfig:
    return SubAgentConfig(
        name="test-coder",
        display_name="Test Coder",
        description="Test coding agent",
        system_prompt="You are a test coder. Write code.",
        model="gpt-4o",
        tools=["python"],
        temperature=0.1,
        max_turns=5,
    )


@pytest.fixture
def agent_middlewares() -> list[HarnessAgentMiddleware]:
    return []


@pytest.fixture
def file_memory_storage(tmp_path) -> FileMemoryStorage:
    return FileMemoryStorage(str(tmp_path / "memory"))


@pytest.fixture
def token_usage() -> TokenUsage:
    return TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150, cost_usd=0.005)
