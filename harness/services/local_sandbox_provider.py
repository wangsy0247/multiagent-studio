"""Local filesystem sandbox provider with DeerFlow-style virtual paths.

This provider maps the virtual path namespace used by agents
(``/mnt/user-data/...`` and ``/mnt/acp-workspace/...``) to host filesystem
paths under the configured data root. It is useful when Docker is not
available, but provides no real process isolation. Use it only in trusted
environments.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.config.paths import (
    ACP_WORKSPACE_VIRTUAL_PATH,
    VIRTUAL_PATH_PREFIX,
    get_paths,
)
from harness.services.sandbox_provider import Sandbox, SandboxProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PathMapping:
    """Mapping from a virtual/container path to a local host path."""

    container_path: str
    local_path: Path
    read_only: bool = False


class LocalSandbox(Sandbox):
    """Sandbox implementation that operates on the local filesystem.

    Agents address files through virtual paths such as
    ``/mnt/user-data/workspace/foo.txt``.  LocalSandbox resolves those to host
    paths under ``{data_root}/users/{user_id}/threads/{thread_id}/`` using
    PathMappings.
    """

    def __init__(self, thread_id: str, *, user_id: str | None = None):
        self.thread_id = thread_id
        self.user_id = user_id
        self.path_mappings = self._build_path_mappings()

    def _build_path_mappings(self) -> list[PathMapping]:
        """Build virtual-to-local path mappings for this thread."""
        paths = get_paths()
        paths.ensure_thread_dirs(self.thread_id, user_id=self.user_id)

        return [
            PathMapping(
                container_path=f"{VIRTUAL_PATH_PREFIX}/workspace",
                local_path=paths.sandbox_work_dir(self.thread_id, user_id=self.user_id),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{VIRTUAL_PATH_PREFIX}/uploads",
                local_path=paths.sandbox_uploads_dir(self.thread_id, user_id=self.user_id),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{VIRTUAL_PATH_PREFIX}/outputs",
                local_path=paths.sandbox_outputs_dir(self.thread_id, user_id=self.user_id),
                read_only=False,
            ),
            PathMapping(
                container_path=VIRTUAL_PATH_PREFIX,
                local_path=paths.sandbox_user_data_dir(self.thread_id, user_id=self.user_id),
                read_only=False,
            ),
            PathMapping(
                container_path=ACP_WORKSPACE_VIRTUAL_PATH,
                local_path=paths.acp_workspace_dir(self.thread_id, user_id=self.user_id),
                read_only=False,
            ),
        ]

    def _find_mapping(self, virtual_path: str) -> PathMapping | None:
        """Find the most specific mapping for a virtual path."""
        for mapping in sorted(
            self.path_mappings, key=lambda m: len(m.container_path), reverse=True
        ):
            container = mapping.container_path
            if virtual_path == container or virtual_path.startswith(f"{container}/"):
                return mapping
        return None

    def resolve_path(self, virtual_path: str) -> str:
        """Resolve a virtual path to a host filesystem path."""
        if virtual_path.startswith("/") and not self._find_mapping(virtual_path):
            raise ValueError(
                f"Path '{virtual_path}' is not under a known virtual prefix"
            )

        mapping = self._find_mapping(virtual_path)
        if mapping is None:
            mapping = self.path_mappings[0]  # /mnt/user-data/workspace
            relative = virtual_path
        else:
            relative = virtual_path[len(mapping.container_path) :].lstrip("/")

        target = (mapping.local_path / relative).resolve()
        try:
            target.relative_to(mapping.local_path.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Path '{virtual_path}' escapes mounted directory '{mapping.container_path}'"
            ) from exc

        return str(target)

    def _reverse_resolve(self, physical_path: str) -> str:
        """Reverse-resolve a host path back to a virtual path."""
        physical = Path(physical_path).resolve()
        for mapping in sorted(
            self.path_mappings, key=lambda m: len(str(m.local_path)), reverse=True
        ):
            local_resolved = mapping.local_path.resolve()
            try:
                relative = physical.relative_to(local_resolved)
            except ValueError:
                continue
            relative_str = str(relative).replace("\\", "/")
            if relative_str == ".":
                return mapping.container_path
            return f"{mapping.container_path}/{relative_str}"
        return physical_path

    def sanitize_output(self, output: str) -> str:
        """Mask host paths in output by reverse-resolving them to virtual paths."""
        if not output:
            return output

        result = output
        for mapping in sorted(
            self.path_mappings, key=lambda m: len(str(m.local_path)), reverse=True
        ):
            local_str = str(mapping.local_path.resolve())
            escaped = re.escape(local_str).replace(r"\\", r"[/\\]")
            pattern = re.compile(escaped + r"(?:[/\\][^\s\"';&|<>()]*)?")

            def replace_match(match: re.Match, _mapping: PathMapping = mapping) -> str:
                matched = match.group(0)
                return self._reverse_resolve(matched)

            result = pattern.sub(replace_match, result)

        result = result.replace(str(get_paths().base_dir.resolve()), "<data-root>")
        result = result.replace(os.path.expanduser("~"), "~")
        return result

    async def execute_command(self, command: str | list[str], timeout: int = 30) -> str:
        if isinstance(command, list):
            command = " ".join(command)

        resolved_command = self._resolve_paths_in_command(command)
        workspace = self.resolve_path(f"{VIRTUAL_PATH_PREFIX}/workspace")

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    resolved_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=workspace,
                ),
                timeout=timeout,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return f"[error] command timed out after {timeout}s"
        except Exception as exc:
            logger.warning("Local command execution failed: %s", exc)
            return f"[error] {exc}"

    def _resolve_paths_in_command(self, command: str) -> str:
        """Replace virtual paths in a shell command with host paths."""
        result = command
        for mapping in sorted(
            self.path_mappings, key=lambda m: len(m.container_path), reverse=True
        ):
            container = mapping.container_path
            pattern = re.compile(
                re.escape(container) + r"(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?"
            )

            def replace_match(match: re.Match, _mapping: PathMapping = mapping) -> str:
                matched = match.group(0)
                return self.resolve_path(matched)

            result = pattern.sub(replace_match, result)
        return result

    async def read_file(self, path: str) -> str:
        target = Path(self.resolve_path(path))
        if not target.exists():
            raise FileNotFoundError(f"file not found: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        target = Path(self.resolve_path(path))
        mapping = self._find_mapping(path)
        if mapping and mapping.read_only:
            raise OSError(f"read-only path: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def list_dir(self, path: str) -> list[str]:
        target = Path(self.resolve_path(path))
        if not target.exists():
            raise FileNotFoundError(f"directory not found: {path}")
        lines: list[str] = []
        for item in sorted(target.iterdir()):
            marker = "dir " if item.is_dir() else "file"
            rel = item.relative_to(target if target.is_dir() else target.parent)
            lines.append(f"{marker}: {rel}")
        return lines

    async def glob(self, path: str, pattern: str) -> list[str]:
        """Return matching paths under ``path`` for ``pattern``."""
        target = Path(self.resolve_path(path))
        if not target.exists():
            raise FileNotFoundError(f"directory not found: {path}")

        base_virtual = self._reverse_resolve(str(target))
        matches: list[str] = []
        for root, dirs, files in os.walk(target):
            root_path = Path(root)
            rel_root = root_path.relative_to(target)
            for name in files + dirs:
                full = root_path / name
                rel = rel_root / name
                rel_str = str(rel).replace("\\", "/")
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_str, pattern):
                    marker = "/" if full.is_dir() else ""
                    matches.append(
                        f"{base_virtual}/{rel_str}{marker}"
                        if rel_str != "."
                        else f"{base_virtual}{marker}"
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
        target = Path(self.resolve_path(path))
        if not target.exists():
            raise FileNotFoundError(f"directory not found: {path}")

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid grep pattern: {exc}")

        base_virtual = self._reverse_resolve(str(target))
        matches: list[tuple[str, int, str]] = []
        for root, dirs, files in os.walk(target):
            for name in files:
                file_path = Path(root) / name
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                rel = file_path.relative_to(target)
                rel_str = str(rel).replace("\\", "/")
                virtual_path = f"{base_virtual}/{rel_str}"
                for line_no, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append((virtual_path, line_no, line))
                        if len(matches) >= max_results:
                            return matches
        return matches


class LocalSandboxProvider(SandboxProvider):
    """Provider that returns local-filesystem sandboxes."""

    def __init__(self, **kwargs: Any):
        self._kwargs = kwargs

    async def acquire(
        self, thread_id: str, workspace: str, *, user_id: str | None = None
    ) -> Sandbox:
        return LocalSandbox(thread_id, user_id=user_id)

    async def release(self, sandbox: Sandbox) -> None:
        pass
