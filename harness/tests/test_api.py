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

    async def execute(self, thread_id, user_id, message, graph=None, files=None,
                      project_id=None, agent_name="default", mode="single",
                      unattended=False):
        yield {"type": "message", "content": "Hello!", "thread_id": thread_id}
        yield {"type": "finished", "thread_id": thread_id}

    async def respond_to_clarification(self, thread_id, answer):
        yield {"type": "message", "content": "", "thread_id": thread_id, "status": "resumed"}
        yield {"type": "finished", "thread_id": thread_id}

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

    def test_execute_midstream_exception_yields_error_event(self, client):
        """生成器中途抛异常时, 流必须正常终止并附带可解析的 error 事件
        (不再以断 TCP / incomplete chunked read 的形式失败)。"""
        class ExplodingHarness(FakeHarness):
            async def execute(self, thread_id, user_id, message, **kwargs):
                yield {"type": "message", "content": "partial", "thread_id": thread_id}
                raise RuntimeError("boom mid-stream")

        set_harness(ExplodingHarness())
        response = TestClient(app).post("/api/v1/execute", json={
            "thread_id": "t2",
            "user_id": "u1",
            "message": "Hi",
        })
        assert response.status_code == 200
        body = response.text
        assert "partial" in body
        assert '"type": "error"' in body
        assert "boom mid-stream" in body

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
            "answer": "yes",
        })
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "resumed" in response.text
        assert "finished" in response.text


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
        # 先创建再删除 (create 写真实用户目录, delete 自清)
        create = client.post("/api/v1/agents", json={"name": "test-agent"})
        assert create.status_code == 200
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
