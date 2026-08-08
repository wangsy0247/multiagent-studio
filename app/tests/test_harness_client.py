"""HarnessClient.stream_execute payload 透传测试 (plan_mode 等)。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.harness_client import HarnessClient


def _sse_body(events: list[dict]) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


@pytest.mark.asyncio
async def test_stream_execute_forwards_plan_mode():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_sse_body([{"type": "finished"}]))

    client = HarnessClient(base_url="http://harness.test")
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(None)
    )

    events = [
        e
        async for e in client.stream_execute(
            thread_id="t1",
            user_id="u1",
            message="hello",
            plan_mode=True,
        )
    ]
    await client.close()

    assert events == ['{"type": "finished"}']
    assert captured["payload"]["plan_mode"] is True
    assert captured["payload"]["mode"] == "single"


@pytest.mark.asyncio
async def test_stream_execute_omits_plan_mode_when_off():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_sse_body([{"type": "finished"}]))

    client = HarnessClient(base_url="http://harness.test")
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(None)
    )

    async for _ in client.stream_execute(thread_id="t1", user_id="u1", message="hello"):
        pass
    await client.close()

    assert "plan_mode" not in captured["payload"]
