"""API integration tests using FastAPI TestClient."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from harness.api.server import app, set_harness, HarnessService


class FakeHarness(HarnessService):
    """Mock Harness for API testing."""

    def __init__(self):
        self.tool_registry = MagicMock()
        self.subagent_manager = MagicMock()
        self.subagent_manager.delete = AsyncMock()
        self.observability = MagicMock()
        self._initialized = True

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def execute(self, thread_id, user_id, message, graph=None):
        yield {"type": "message", "content": "Hello!", "thread_id": thread_id}
        yield {"type": "finished", "thread_id": thread_id}

    async def respond_to_clarification(self, thread_id, clarification_id, answer):
        return {"status": "resumed", "thread_id": thread_id}

    async def stop(self, thread_id):
        pass

    async def get_status(self, thread_id):
        return {"thread_id": thread_id, "status": "running"}


@pytest.fixture
def client():
    harness = FakeHarness()
    harness.tool_registry.setup_tool_groups.return_value = {"search": {}}
    harness.subagent_manager.list.return_value = []
    harness.observability.get_trace.return_value = {"trace_id": "t1", "enabled": False}
    harness.observability.get_token_usage.return_value = {"items": []}
    set_harness(harness)
    return TestClient(app)


class TestExecutionAPI:
    def test_execute_returns_sse_stream(self, client):
        response = client.post("/api/v1/execute", json={
            "thread_id": "t1",
            "user_id": "u1",
            "message": "Hello",
        })
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.text
        assert "Hello!" in body
        assert "finished" in body

    def test_stop_execution(self, client):
        response = client.post("/api/v1/stop/t1")
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"

    def test_get_status(self, client):
        response = client.get("/api/v1/status/t1")
        assert response.status_code == 200
        data = response.json()
        assert data["thread_id"] == "t1"
        assert data["status"] == "running"

    def test_respond_clarification(self, client):
        response = client.post("/api/v1/execute/t1/respond", json={
            "clarification_id": "clar-001",
            "answer": "yes",
        })
        assert response.status_code == 200
        assert response.json()["status"] == "resumed"


class TestAgentManagementAPI:
    def test_list_agents(self, client):
        response = client.get("/api/v1/agents")
        assert response.status_code == 200

    def test_get_preset_agents(self, client):
        response = client.get("/api/v1/agents/presets")
        assert response.status_code == 200
        data = response.json()
        assert "researcher" in data
        assert "coder" in data

    def test_delete_agent(self, client):
        response = client.delete("/api/v1/agents/test-agent")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"


class TestObservabilityAPI:
    def test_get_trace(self, client):
        response = client.get("/api/v1/traces/t1")
        assert response.status_code == 200
        assert response.json()["trace_id"] == "t1"

    def test_get_token_usage(self, client):
        response = client.get("/api/v1/metrics/token-usage")
        assert response.status_code == 200

    def test_get_token_usage_filtered(self, client):
        response = client.get("/api/v1/metrics/token-usage?user_id=u1&start_date=2025-01-01&end_date=2025-12-31")
        assert response.status_code == 200


class TestToolGroupsAPI:
    def test_get_tool_groups(self, client):
        response = client.get("/api/v1/tool-groups")
        assert response.status_code == 200
