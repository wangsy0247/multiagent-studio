"""Sandbox-backed file and shell tools (DeerFlow-style).

Agents address files through virtual paths such as
``/mnt/user-data/workspace/foo.txt``. The configured ``SandboxProvider``
resolves those to host or container paths transparently.
"""
from __future__ import annotations

import contextvars
import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from harness.services.sandbox_provider import Sandbox, get_sandbox_provider

logger = logging.getLogger(__name__)

_tool_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "harness_sandbox_tool_ctx", default={}
)


def set_sandbox_tool_context(
    workspace: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Set runtime context for sandbox-backed tools."""
    _tool_ctx.set({"workspace": workspace, "thread_id": thread_id, "user_id": user_id})


def _current_ctx() -> dict[str, Any]:
    return _tool_ctx.get()


async def _get_sandbox() -> Sandbox:
    """Acquire a sandbox instance from the configured provider."""
    ctx = _current_ctx()
    workspace = ctx.get("workspace") or "."
    thread_id = ctx.get("thread_id") or "default"
    user_id = ctx.get("user_id")
    provider = get_sandbox_provider()
    return await provider.acquire(thread_id, workspace, user_id=user_id)


def _normalize_virtual_path(path: str) -> str:
    """Normalize a user-supplied path to a virtual path.

    Absolute paths that already use the virtual namespace are returned as-is.
    Relative paths are treated as workspace-relative.
    """
    from harness.config.paths import VIRTUAL_PATH_PREFIX

    path = path.strip()
    if path.startswith(VIRTUAL_PATH_PREFIX) or path.startswith("/mnt/acp-workspace"):
        return path
    if path.startswith("/"):
        # Reject host absolute paths to prevent accidental host access.
        raise ValueError(
            f"Absolute host paths are not allowed: {path}. "
            f"Use virtual paths like {VIRTUAL_PATH_PREFIX}/workspace/..."
        )
    # Treat as workspace-relative.
    path = path.lstrip("./")
    return f"{VIRTUAL_PATH_PREFIX}/workspace/{path}"


def create_bash_tool() -> BaseTool:
    """Create the ``bash`` tool backed by the configured sandbox provider."""

    @tool
    async def bash(command: str, timeout: int = 30) -> str:
        """Execute a shell command in the isolated sandbox.

        Args:
            command: Shell command to execute. Refer to files using virtual
                paths such as ``/mnt/user-data/workspace/foo.txt``.
            timeout: Maximum execution time in seconds.
        """
        try:
            sandbox = await _get_sandbox()
            output = await sandbox.execute_command(command, timeout=timeout)
            return sandbox.sanitize_output(output)
        except Exception as exc:
            logger.warning("Sandbox bash execution failed: %s", exc)
            return f"[error] sandbox execution failed: {exc}"

    return bash


def create_file_read_tool() -> BaseTool:
    """Create the ``file_read`` tool."""

    @tool
    async def file_read(path: str) -> str:
        """Read a file from the workspace.

        Args:
            path: Virtual path such as ``/mnt/user-data/workspace/foo.txt``.
                Relative paths are treated as workspace-relative.
        """
        try:
            virtual_path = _normalize_virtual_path(path)
            sandbox = await _get_sandbox()
            content = await sandbox.read_file(virtual_path)
            return sandbox.sanitize_output(content)
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except Exception as exc:
            logger.warning("file_read failed: %s", exc)
            return f"[error] {exc}"

    return file_read


def create_file_write_tool() -> BaseTool:
    """Create the ``file_write`` tool."""

    @tool
    async def file_write(path: str, content: str) -> str:
        """Write content to a file in the workspace.

        Args:
            path: Virtual path such as ``/mnt/user-data/workspace/foo.txt``.
                Relative paths are treated as workspace-relative.
            content: Text content to write.
        """
        try:
            virtual_path = _normalize_virtual_path(path)
            sandbox = await _get_sandbox()
            await sandbox.write_file(virtual_path, content)
            return f"[ok] wrote {len(content)} bytes to {virtual_path}"
        except Exception as exc:
            logger.warning("file_write failed: %s", exc)
            return f"[error] {exc}"

    return file_write


def create_list_files_tool() -> BaseTool:
    """Create the ``list_files`` tool."""

    @tool
    async def list_files(directory: str = ".") -> str:
        """List files in a workspace directory.

        Args:
            directory: Virtual directory path. Defaults to workspace root.
        """
        try:
            virtual_path = _normalize_virtual_path(directory)
            sandbox = await _get_sandbox()
            lines = await sandbox.list_dir(virtual_path)
            output = "\n".join(lines) or "(empty directory)"
            return sandbox.sanitize_output(output)
        except FileNotFoundError:
            return f"[error] directory not found: {directory}"
        except Exception as exc:
            logger.warning("list_files failed: %s", exc)
            return f"[error] {exc}"

    return list_files


def create_glob_tool() -> BaseTool:
    """Create the ``glob`` tool for finding files by pattern."""

    @tool
    async def glob_tool(pattern: str, directory: str = ".") -> str:
        """Find files and directories matching ``pattern`` under ``directory``.

        Args:
            pattern: Glob pattern (e.g. ``*.py``).
            directory: Virtual directory path. Defaults to workspace root.
        """
        try:
            virtual_path = _normalize_virtual_path(directory)
            sandbox = await _get_sandbox()
            matches = await sandbox.glob(virtual_path, pattern)
            if not matches:
                return "(no matches)"
            output = "\n".join(matches)
            return sandbox.sanitize_output(output)
        except FileNotFoundError:
            return f"[error] directory not found: {directory}"
        except Exception as exc:
            logger.warning("glob failed: %s", exc)
            return f"[error] {exc}"

    return glob_tool


def create_grep_tool() -> BaseTool:
    """Create the ``grep`` tool for searching file contents."""

    @tool
    async def grep_tool(
        pattern: str,
        path: str = ".",
        *,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> str:
        """Search file contents under ``path`` for ``pattern``.

        Args:
            pattern: Regular expression or literal string to search for.
            path: Virtual directory or file path. Defaults to workspace root.
            case_sensitive: Whether matching is case-sensitive.
            max_results: Maximum number of matching lines to return.
        """
        try:
            virtual_path = _normalize_virtual_path(path)
            sandbox = await _get_sandbox()
            matches = await sandbox.grep(
                virtual_path,
                pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
            if not matches:
                return "(no matches)"
            lines = [f"{p}:{ln}: {line}" for p, ln, line in matches]
            output = "\n".join(lines)
            return sandbox.sanitize_output(output)
        except FileNotFoundError:
            return f"[error] path not found: {path}"
        except Exception as exc:
            logger.warning("grep failed: %s", exc)
            return f"[error] {exc}"

    return grep_tool


def create_str_replace_tool() -> BaseTool:
    """Create the ``str_replace`` tool for in-place text replacement."""

    @tool
    async def str_replace(
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> str:
        """Replace ``old_str`` with ``new_str`` in a workspace file.

        Args:
            path: Virtual path such as ``/mnt/user-data/workspace/foo.txt``.
                Relative paths are treated as workspace-relative.
            old_str: Exact text to replace.
            new_str: Replacement text.
            replace_all: If True, replace all occurrences; otherwise replace only
                the first occurrence.
        """
        try:
            virtual_path = _normalize_virtual_path(path)
            sandbox = await _get_sandbox()
            content = await sandbox.read_file(virtual_path)
            if old_str not in content:
                return f"[error] old_str not found in {virtual_path}"

            count = -1 if replace_all else 1
            new_content = content.replace(old_str, new_str, count)
            await sandbox.write_file(virtual_path, new_content)
            return f"[ok] replaced in {virtual_path}"
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except Exception as exc:
            logger.warning("str_replace failed: %s", exc)
            return f"[error] {exc}"

    return str_replace


def build_sandbox_tools(
    workspace: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> list[BaseTool]:
    """Return all sandbox-backed file/shell tools bound to a workspace/thread."""
    set_sandbox_tool_context(workspace=workspace, thread_id=thread_id, user_id=user_id)
    return [
        create_bash_tool(),
        create_file_read_tool(),
        create_file_write_tool(),
        create_list_files_tool(),
        create_glob_tool(),
        create_grep_tool(),
        create_str_replace_tool(),
    ]


# Module-level convenience instances (use current context).
bash = create_bash_tool()
file_read = create_file_read_tool()
file_write = create_file_write_tool()
list_files = create_list_files_tool()
glob_tool = create_glob_tool()
grep_tool = create_grep_tool()
str_replace = create_str_replace_tool()
