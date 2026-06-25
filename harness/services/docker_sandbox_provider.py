"""Docker sandbox provider with DeerFlow-style multi-point bind mounts.

Wraps SandboxService and exposes the same virtual path namespace as
LocalSandboxProvider so that agents can use ``/mnt/user-data/workspace/...``
regardless of the configured backend.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any

from harness.config.paths import (
    ACP_WORKSPACE_VIRTUAL_PATH,
    VIRTUAL_PATH_PREFIX,
    get_paths,
)
from harness.services.sandbox import SandboxService
from harness.services.sandbox_provider import Sandbox, SandboxProvider

logger = logging.getLogger(__name__)


class DockerSandbox(Sandbox):
    """Sandbox implementation backed by a Docker container."""

    def __init__(self, thread_id: str, service: SandboxService, *, user_id: str | None = None):
        self.thread_id = thread_id
        self.user_id = user_id
        self.service = service

    def resolve_path(self, virtual_path: str) -> str:
        """Return the in-container path for a virtual path."""
        if not virtual_path.startswith((VIRTUAL_PATH_PREFIX, ACP_WORKSPACE_VIRTUAL_PATH)):
            if not virtual_path.startswith("/"):
                return f"{VIRTUAL_PATH_PREFIX}/workspace/{virtual_path}"
            raise ValueError(
                f"Path '{virtual_path}' is not under a known virtual prefix"
            )
        return virtual_path

    def sanitize_output(self, output: str) -> str:
        """Sanitize command output."""
        if not output:
            return output
        result = output.replace(os.path.expanduser("~"), "~")
        result = result.replace(str(get_paths().base_dir.resolve()), "<data-root>")
        return result

    def _container_path(self, virtual_path: str) -> str:
        return self.resolve_path(virtual_path)

    async def execute_command(self, command: str | list[str], timeout: int = 30) -> str:
        return await self.service.execute(self.thread_id, command, timeout=timeout)

    async def read_file(self, path: str) -> str:
        container_path = self._container_path(path)
        result = await self.service.execute(
            self.thread_id, f"cat {shlex.quote(container_path)}", timeout=30
        )
        if result.startswith("[error]") or (
            result.startswith("[exit:") and "No such file" in result
        ):
            raise FileNotFoundError(f"file not found: {path}")
        if result.startswith("[exit:"):
            idx = result.find("\n")
            if idx != -1:
                return result[idx + 1 :]
        return result

    async def write_file(self, path: str, content: str) -> None:
        container_path = self._container_path(path)
        marker = "EOF_HARNESS_WRITE"
        escaped = content.replace(marker, f"{marker}_")
        command = (
            f"mkdir -p $(dirname {shlex.quote(container_path)}) && "
            f"cat > {shlex.quote(container_path)} << '{marker}'\n{escaped}\n{marker}"
        )
        result = await self.service.execute(self.thread_id, command, timeout=30)
        if result.startswith("[error]"):
            raise RuntimeError(f"failed to write file {path}: {result}")

    async def list_dir(self, path: str) -> list[str]:
        container_path = self._container_path(path)
        result = await self.service.execute(
            self.thread_id,
            f"ls -la {shlex.quote(container_path)}",
            timeout=30,
        )
        if "No such file" in result or "cannot access" in result:
            raise FileNotFoundError(f"directory not found: {path}")

        lines = result.splitlines()
        output: list[str] = []
        for line in lines:
            if line.startswith("total "):
                continue
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue
            name = parts[-1]
            if name in (".", ".."):
                continue
            kind = parts[0][0]
            marker = "dir " if kind == "d" else "file"
            output.append(f"{marker}: {name}")
        return output

    async def glob(self, path: str, pattern: str) -> list[str]:
        """Return matching paths under ``path`` for ``pattern``."""
        container_path = self._container_path(path)
        result = await self.service.execute(
            self.thread_id,
            f"find {shlex.quote(container_path)} -mindepth 1 \\( -type f -o -type d \\) 2>/dev/null",
            timeout=30,
        )
        if result.startswith("[error]"):
            raise RuntimeError(f"glob failed: {result}")

        matches: list[str] = []
        prefix = container_path.rstrip("/")
        for line in result.splitlines():
            line = line.strip()
            if not line or line.startswith("[exit:"):
                continue
            rel = line[len(prefix) + 1 :] if line.startswith(prefix + "/") else line
            name = rel.split("/")[-1]
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                matches.append(
                    f"{container_path}/{rel}" if rel else container_path
                )
        return sorted(set(matches))

    async def grep(
        self,
        path: str,
        pattern: str,
        *,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> list[tuple[str, int, str]]:
        """Search ``pattern`` under ``path`` and return matching lines."""
        container_path = self._container_path(path)
        flag = "" if case_sensitive else "-i"
        result = await self.service.execute(
            self.thread_id,
            f"grep -rn {flag} -m {max_results} -e {shlex.quote(pattern)} {shlex.quote(container_path)} 2>/dev/null || true",
            timeout=30,
        )
        if result.startswith("[error]"):
            raise RuntimeError(f"grep failed: {result}")

        matches: list[tuple[str, int, str]] = []
        prefix = container_path.rstrip("/")
        for line in result.splitlines():
            line = line.strip()
            if not line or line.startswith("[exit:"):
                continue
            if ":" not in line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            raw_path, line_no_str, content = parts
            rel = raw_path[len(prefix) + 1 :] if raw_path.startswith(prefix + "/") else raw_path
            virtual_path = f"{container_path}/{rel}" if rel else container_path
            try:
                line_no = int(line_no_str)
            except ValueError:
                line_no = 0
            matches.append((virtual_path, line_no, content))
            if len(matches) >= max_results:
                break
        return matches


class DockerSandboxProvider(SandboxProvider):
    """Provider that manages Docker container sandboxes per thread."""

    def __init__(
        self,
        image: str = "python:3.11-slim",
        mem_limit: str = "512m",
        cpu_quota: int = 100000,
    ):
        self.service = SandboxService(
            image=image,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
        )

    def _get_thread_mounts(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[tuple[str, str, bool]]:
        """Build DeerFlow-style bind mounts for a thread."""
        paths = get_paths()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)

        return [
            (paths.host_sandbox_work_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/workspace", False),
            (paths.host_sandbox_uploads_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/uploads", False),
            (paths.host_sandbox_outputs_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/outputs", False),
            (paths.host_acp_workspace_dir(thread_id, user_id=user_id), ACP_WORKSPACE_VIRTUAL_PATH, True),
        ]

    async def acquire(
        self, thread_id: str, workspace: str, *, user_id: str | None = None
    ) -> Sandbox:
        mounts = self._get_thread_mounts(thread_id, user_id=user_id)
        await self.service.get_or_create(thread_id, workspace, mounts=mounts)
        return DockerSandbox(thread_id, self.service, user_id=user_id)

    async def release(self, sandbox: Sandbox) -> None:
        if isinstance(sandbox, DockerSandbox):
            await self.service.cleanup(sandbox.thread_id)
