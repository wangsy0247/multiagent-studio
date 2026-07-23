"""OpenSandbox provider with DeerFlow-style multi-point bind mounts.

Wraps the OpenSandbox Python SDK and exposes the same virtual path namespace as
LocalSandboxProvider so that agents can use ``/mnt/user-data/...`` regardless of
the configured backend.
"""
from __future__ import annotations

import logging
import os
import shlex
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opensandbox import Sandbox as OpenSandboxClient
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import SearchEntry, WriteEntry
from opensandbox.models.sandboxes import Host, Volume

from harness.config.paths import (
    ACP_WORKSPACE_VIRTUAL_PATH,
    VIRTUAL_PATH_PREFIX,
    VIRTUAL_SKILLS_PATH,
    get_paths,
    get_skills_root,
)
from harness.services.sandbox_provider import Sandbox, SandboxProvider

logger = logging.getLogger(__name__)


def _execution_output_text(execution: Any) -> str:
    """Flatten OpenSandbox execution logs into a single string."""
    chunks: list[str] = []
    logs = execution.logs
    if logs:
        for entry in logs.stdout or []:
            if entry.text:
                chunks.append(entry.text)
        for entry in logs.stderr or []:
            if entry.text:
                chunks.append(entry.text)
    return "\n".join(chunks)


class OpenSandbox(Sandbox):
    """Sandbox implementation backed by an OpenSandbox instance."""

    def __init__(self, thread_id: str, sbx: OpenSandboxClient, *, user_id: str | None = None):
        self.thread_id = thread_id
        self.user_id = user_id
        self._sbx = sbx

    def resolve_path(self, virtual_path: str) -> str:
        """Return the in-container path for a virtual path."""
        # Allow /mnt/skills paths (single volume mount, read-only).
        if virtual_path.startswith((VIRTUAL_PATH_PREFIX, ACP_WORKSPACE_VIRTUAL_PATH, VIRTUAL_SKILLS_PATH)):
            return virtual_path
        if not virtual_path.startswith("/"):
            return f"{VIRTUAL_PATH_PREFIX}/workspace/{virtual_path}"
        raise ValueError(
            f"Path '{virtual_path}' is not under a known virtual prefix"
        )

    def sanitize_output(self, output: str) -> str:
        """Sanitize command output by masking physical paths."""
        if not output:
            return output
        result = output.replace(os.path.expanduser("~"), "~")
        result = result.replace(str(get_paths().base_dir.resolve()), "<data-root>")
        return result

    def _container_path(self, virtual_path: str) -> str:
        return self.resolve_path(virtual_path)

    async def execute_command(
        self, command: str | list[str], timeout: int = 30, cwd: str = "",
    ) -> str:
        if isinstance(command, list):
            command = shlex.join(command)
        # Prepend cd if a working directory is specified.
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        try:
            execution = await self._sbx.commands.run(
                command,
                opts=RunCommandOpts(timeout=timedelta(seconds=timeout)),
            )
        except Exception as exc:
            return f"[error] {exc}"

        output = _execution_output_text(execution)
        exit_code = execution.exit_code if execution.exit_code is not None else -1
        return f"[exit:{exit_code}]\n{output}"

    async def read_file(self, path: str) -> str:
        container_path = self._container_path(path)
        try:
            return await self._sbx.files.read_file(container_path)
        except Exception as exc:
            err = str(exc).lower()
            if "not found" in err or "no such file" in err or "404" in err:
                raise FileNotFoundError(f"file not found: {path}") from exc
            raise

    async def write_file(self, path: str, content: str) -> None:
        container_path = self._container_path(path)
        try:
            await self._sbx.files.write_files(
                [WriteEntry(path=container_path, data=content, mode=644)]
            )
        except Exception as exc:
            raise RuntimeError(f"failed to write file {path}: {exc}") from exc

    async def list_dir(self, path: str) -> list[str]:
        container_path = self._container_path(path)
        try:
            entries = await self._sbx.files.search(
                SearchEntry(path=container_path, pattern="*")
            )
        except Exception as exc:
            err = str(exc).lower()
            if "not found" in err or "no such file" in err or "404" in err:
                raise FileNotFoundError(f"directory not found: {path}") from exc
            raise

        output: list[str] = []
        container_path_norm = container_path.rstrip("/")
        for entry in entries:
            entry_path = str(entry.path).rstrip("/")
            if entry_path == container_path_norm:
                continue
            name = Path(entry_path).name
            if not name or name in (".", ".."):
                continue
            is_dir = entry.entry_type == "directory" if entry.entry_type else False
            marker = "dir" if is_dir else "file"
            output.append(f"{marker}: {name}")
        return sorted(output)

    async def glob(self, path: str, pattern: str) -> list[str]:
        container_path = self._container_path(path)
        try:
            entries = await self._sbx.files.search(
                SearchEntry(path=container_path, pattern=pattern)
            )
        except Exception as exc:
            return f"[error] glob failed: {exc}"

        matches = {entry.path for entry in entries}
        return sorted(matches)

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
        result = await self.execute_command(
            f"grep -rn {flag} -m {max_results} -e {shlex.quote(pattern)} "
            f"{shlex.quote(container_path)} 2>/dev/null || true"
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


class OpenSandboxProvider(SandboxProvider):
    """Provider that manages OpenSandbox instances per thread."""

    def __init__(
        self,
        image: str = "",  # 空=从 SANDBOX_IMAGE env var 或 config 读取
        server_url: str = "http://localhost:8080",
        api_key: str = "",
        resource_cpu: str = "1",
        resource_memory: str = "2Gi",
        timeout_minutes: int = 30,
    ):
        self.image = image
        self.timeout_minutes = timeout_minutes
        parsed = urlparse(server_url)
        protocol = parsed.scheme or "http"
        domain = parsed.netloc or parsed.path or "localhost:8080"
        self.connection_config = ConnectionConfig(
            domain=domain,
            api_key=api_key or "",
            protocol=protocol,
        )
        self.resource = {"cpu": resource_cpu, "memory": resource_memory}

    def _build_volumes(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[Volume]:
        """Build DeerFlow-style bind mounts for a thread."""
        paths = get_paths()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)

        def _vol(name: str, host_path: str, mount_path: str, read_only: bool = False) -> Volume:
            return Volume(
                name=name,
                host=Host(path=host_path),
                mount_path=mount_path,
                read_only=read_only,
            )

        safe_id = thread_id.replace("/", "_").replace("\\", "_")[:32]
        base_name = f"harness-{safe_id}"

        volumes = [
            _vol(
                f"{base_name}-workspace",
                paths.host_sandbox_work_dir(thread_id, user_id=user_id),
                f"{VIRTUAL_PATH_PREFIX}/workspace",
            ),
            _vol(
                f"{base_name}-uploads",
                paths.host_sandbox_uploads_dir(thread_id, user_id=user_id),
                f"{VIRTUAL_PATH_PREFIX}/uploads",
            ),
            _vol(
                f"{base_name}-outputs",
                paths.host_sandbox_outputs_dir(thread_id, user_id=user_id),
                f"{VIRTUAL_PATH_PREFIX}/outputs",
            ),
            _vol(
                f"{base_name}-acp",
                paths.host_acp_workspace_dir(thread_id, user_id=user_id),
                ACP_WORKSPACE_VIRTUAL_PATH,
                read_only=False,
            ),
        ]

        # ── Skills ──
        # builtin/ → 实际文件副本 (sync_builtin_skills).
        # my/      → 用户私有技能, 独立挂载 (不能用 symlink, 因为:
        #            symlink 的绝对路径目标在容器内不存在, Docker 内核在
        #            容器命名空间中解析 symlink → broken symlink → 404).
        skills_root = get_skills_root()
        if skills_root.exists():
            from harness.config.paths import ensure_user_skills_symlink, sync_builtin_skills
            sync_builtin_skills(skills_root)
            # symlink 仍然维护, 供 LocalSandbox (已修复) 和宿主机工具使用
            ensure_user_skills_symlink(
                skills_root, user_id or "default", _paths=paths,
            )
            volumes.append(
                _vol(
                    f"{base_name}-skills",
                    paths.host_skills_dir,
                    VIRTUAL_SKILLS_PATH,
                    read_only=True,
                )
            )

        # 用户私有技能: 独立挂载到 /mnt/skills/my, 绕过 symlink.
        # Docker 容器内无法跟随指向容器外绝对路径的 symlink, 必须直接挂载.
        user_skills_dir = paths.user_skills_dir(user_id or "default")
        if user_skills_dir.exists():
            volumes.append(
                _vol(
                    f"{base_name}-skills-user",
                    str(user_skills_dir),
                    f"{VIRTUAL_SKILLS_PATH}/my",
                    read_only=True,
                )
            )

        return volumes

    async def acquire(
        self, thread_id: str, workspace: str, *, user_id: str | None = None
    ) -> Sandbox:
        volumes = self._build_volumes(thread_id, user_id=user_id)
        sbx = await OpenSandboxClient.create(
            self.image,
            connection_config=self.connection_config,
            timeout=timedelta(minutes=self.timeout_minutes),
            entrypoint=["/bin/sh", "-c", "sleep infinity"],
            resource=self.resource,
            volumes=volumes,
        )
        return OpenSandbox(thread_id, sbx, user_id=user_id)

    async def release(self, sandbox: Sandbox) -> None:
        if isinstance(sandbox, OpenSandbox):
            try:
                await sandbox._sbx.kill()
                await sandbox._sbx.close()
            except Exception as exc:
                logger.warning("Error releasing OpenSandbox %s: %s", sandbox.thread_id, exc)
