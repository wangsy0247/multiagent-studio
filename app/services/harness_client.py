"""
Harness 服务 HTTP 客户端 — 代理所有执行相关请求
"""

import os
import json
import logging
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger(__name__)

HARNESS_SERVICE_URL = os.getenv("HARNESS_SERVICE_URL", "http://localhost:8001")


class HarnessClient:
    def __init__(self, base_url: str = HARNESS_SERVICE_URL):
        self.base_url = base_url.rstrip("/")
        self.client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(None))
        return self.client

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        url = f"{self.base_url}{path}"
        logger.info(f"HarnessClient {method} {url}")
        try:
            return await client.request(method, url, **kwargs)
        except httpx.ConnectError:
            raise HarnessUnavailableError(f"Harness 服务不可达: {self.base_url}")

    async def stream_execute(
        self,
        thread_id: str,
        user_id: str,
        message: str,
        execution_graph: Optional[dict] = None,
        files: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        """代理执行请求，流式返回 SSE 事件"""
        payload = {
            "thread_id": thread_id,
            "user_id": user_id,
            "message": message,
        }
        if execution_graph:
            payload["execution_graph"] = execution_graph
        if files:
            payload["files"] = files

        client = await self._get_client()
        url = f"{self.base_url}/api/v1/execute"

        async with client.stream("POST", url, json=payload) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise HarnessAPIError(response.status_code, error_body.decode())

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line[6:]  # 去掉 "data: " 前缀
                elif line.strip():
                    yield line

    async def respond_to_clarification(self, thread_id: str, clarification_id: str, answer: str) -> dict:
        """@deprecated — use stream_respond_clarification for streaming results."""
        resp = await self._request(
            "POST",
            f"/api/v1/execute/{thread_id}/respond",
            json={"clarification_id": clarification_id, "answer": answer},
        )
        return resp.json()

    async def stream_respond_clarification(
        self, thread_id: str, clarification_id: str, answer: str
    ) -> AsyncGenerator[str, None]:
        """Stream the resumed execution after a clarification answer."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/execute/{thread_id}/respond"

        async with client.stream("POST", url, json={
            "clarification_id": clarification_id,
            "answer": answer,
        }) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise HarnessAPIError(response.status_code, error_body.decode())

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line[6:]
                elif line.strip():
                    yield line

    async def stop_execution(self, thread_id: str) -> dict:
        resp = await self._request("POST", f"/api/v1/stop/{thread_id}")
        return resp.json()

    async def get_status(self, thread_id: str) -> dict:
        resp = await self._request("GET", f"/api/v1/status/{thread_id}")
        return resp.json()

    async def get_run_events(self, thread_id: str, run_id: str | None = None,
                              event_types: str | None = None, limit: int = 100) -> dict:
        params = {"limit": limit}
        if run_id:
            params["run_id"] = run_id
        if event_types:
            params["event_types"] = event_types
        resp = await self._request("GET", f"/api/v1/runs/{thread_id}/events", params=params)
        return resp.json()

    async def get_agents(self) -> list:
        resp = await self._request("GET", "/api/v1/agents")
        return resp.json()

    async def get_presets(self) -> list:
        resp = await self._request("GET", "/api/v1/agents/presets")
        return resp.json()

    async def get_tool_groups(self) -> list:
        resp = await self._request("GET", "/api/v1/tool-groups")
        return resp.json()

    async def get_trace(self, thread_id: str) -> dict:
        resp = await self._request("GET", f"/api/v1/traces/{thread_id}")
        return resp.json()

    async def get_token_usage(self, **params) -> dict:
        resp = await self._request("GET", "/api/v1/metrics/token-usage", params=params)
        return resp.json()


class HarnessUnavailableError(Exception):
    """Harness 服务不可用"""
    pass


class HarnessAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Harness API {status_code}: {detail}")


# 单例
_harness_client: Optional[HarnessClient] = None


def get_harness_client() -> HarnessClient:
    global _harness_client
    if _harness_client is None:
        _harness_client = HarnessClient()
    return _harness_client
