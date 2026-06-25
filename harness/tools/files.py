"""File operation tools constrained to a workspace."""
from __future__ import annotations

import contextvars
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_tool_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "harness_file_tool_ctx", default={}
)


def set_file_tool_context(workspace: str | None = None) -> None:
    """Set the workspace root for file tools at runtime."""
    _tool_ctx.set({"workspace": workspace})


def _resolve_path(path: str, workspace: str | None = None) -> Path:
    """Resolve a user-supplied path inside the workspace."""
    ctx = _tool_ctx.get()
    base = Path(workspace or ctx.get("workspace") or ".").resolve()
    target = (base / path).resolve()
    # Prevent path traversal outside the workspace
    if base not in target.parents and target != base:
        raise ValueError(f"Path '{path}' escapes workspace")
    return target


def create_file_read_tool(workspace: str | None = None) -> Any:
    """Create the ``file_read`` tool."""

    @tool
    async def file_read(path: str) -> str:
        """Read a file from the workspace.

        Args:
            path: Relative path inside the workspace.
        """
        try:
            target = _resolve_path(path, workspace)
        except ValueError as exc:
            return f"[error] {exc}"

        if not target.exists():
            return f"[error] file not found: {path}"

        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("file_read failed: %s", exc)
            return f"[error] {exc}"

    return file_read


def create_file_write_tool(workspace: str | None = None) -> Any:
    """Create the ``file_write`` tool."""

    @tool
    async def file_write(path: str, content: str) -> str:
        """Write content to a file in the workspace.

        Args:
            path: Relative path inside the workspace.
            content: Text content to write.
        """
        try:
            target = _resolve_path(path, workspace)
        except ValueError as exc:
            return f"[error] {exc}"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"[ok] wrote {len(content)} bytes to {path}"
        except Exception as exc:
            logger.warning("file_write failed: %s", exc)
            return f"[error] {exc}"

    return file_write


def create_list_files_tool(workspace: str | None = None) -> Any:
    """Create the ``list_files`` tool."""

    @tool
    async def list_files(directory: str = ".") -> str:
        """List files in a workspace directory.

        Args:
            directory: Directory path relative to the workspace.
        """
        try:
            target = _resolve_path(directory, workspace)
        except ValueError as exc:
            return f"[error] {exc}"

        if not target.exists():
            return f"[error] directory not found: {directory}"

        try:
            lines = []
            for item in sorted(target.iterdir()):
                marker = "dir " if item.is_dir() else "file"
                rel = item.relative_to(target if target.is_dir() else target.parent)
                lines.append(f"{marker}: {rel}")
            return "\n".join(lines) or "(empty directory)"
        except Exception as exc:
            logger.warning("list_files failed: %s", exc)
            return f"[error] {exc}"

    return list_files


def build_file_tools(workspace: str | None = None) -> list[Any]:
    """Return all file tools bound to a workspace."""
    return [
        create_file_read_tool(workspace),
        create_file_write_tool(workspace),
        create_list_files_tool(workspace),
    ]


# Module-level convenience instances bound to the current working directory.
file_read = create_file_read_tool()
file_write = create_file_write_tool()
list_files = create_list_files_tool()
