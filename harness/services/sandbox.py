"""Docker sandbox service for isolated code execution."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SandboxService:
    """Manage Docker container sandboxes per thread."""

    def __init__(self, image: str = "python:3.11-slim", mem_limit: str = "512m", cpu_quota: int = 100000):
        self.image = image
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self._pool: dict[str, Any] = {}
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import docker
                self._client = docker.from_env()
            except Exception as exc:
                logger.warning("Docker not available: %s", exc)
        return self._client

    async def get_or_create(self, thread_id: str, workspace: str) -> Any:
        if thread_id in self._pool:
            return self._pool[thread_id]

        # ── 修复 #10: 容器池上限，淘汰最旧容器 ──
        MAX_CONTAINERS = 50
        if len(self._pool) >= MAX_CONTAINERS:
            oldest_tid = next(iter(self._pool))
            logger.warning("Container pool full (%d), evicting oldest: %s", MAX_CONTAINERS, oldest_tid)
            await self.cleanup(oldest_tid)

        client = self._get_client()
        if client is None:
            return None

        # Docker requires absolute host paths for volume mounts
        host_workspace = str(Path(workspace).resolve())
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.run(
                image=self.image,
                command="sleep infinity",
                volumes={host_workspace: {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                network_mode="bridge",
                detach=True,
                remove=True,
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
            ),
        )
        self._pool[thread_id] = container
        return container

    async def execute(self, thread_id: str, command: list[str] | str, timeout: int = 30) -> str:
        container = self._pool.get(thread_id)
        if container is None:
            return "[error] sandbox not initialized"

        loop = asyncio.get_event_loop()
        try:
            if isinstance(command, str):
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: container.exec_run(command)),
                    timeout=timeout,
                )
            else:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: container.exec_run(command)),
                    timeout=timeout,
                )
            exit_code, output = result
            text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
            return f"[exit:{exit_code}]\n{text}"
        except asyncio.TimeoutError:
            return f"[error] command timed out after {timeout}s"
        except Exception as exc:
            return f"[error] {exc}"

    async def cleanup(self, thread_id: str | None = None) -> None:
        if thread_id:
            container = self._pool.pop(thread_id, None)
            if container:
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, container.stop)
                except Exception as exc:
                    logger.warning("Error stopping container: %s", exc)
        else:
            for tid in list(self._pool):
                await self.cleanup(tid)
