"""UploadsMiddleware — inject uploaded file context into the agent prompt.

Mirrors DeerFlow's uploads middleware: reads file metadata from the latest
HumanMessage's ``additional_kwargs.files`` and scans the thread's uploads
directory for historical files. The resulting ``<uploaded_files>`` block is
prepended to the latest human message so the model knows which files are
available and how to read them.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import override

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from harness.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_OUTLINE_PREVIEW_LINES = 5


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    kb = size_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def _extract_outline_for_file(file_path: Path) -> tuple[list[dict], list[str]]:
    """Return (outline, fallback_preview) for a file's companion .md.

    Looks for ``<stem>.md`` next to the original file (e.g. report.pdf → report.md)
    produced by an upload conversion pipeline.
    """
    md_path = file_path.with_suffix(".md")
    if not md_path.is_file():
        return [], []

    outline: list[dict] = []
    try:
        with md_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    title = stripped.lstrip("#").strip()
                    outline.append({"title": title, "line": line_no})
    except Exception:
        logger.debug("Failed to extract outline from %s", md_path, exc_info=True)

    if outline:
        return outline, []

    preview: list[str] = []
    try:
        with md_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    preview.append(stripped)
                if len(preview) >= _OUTLINE_PREVIEW_LINES:
                    break
    except Exception:
        logger.debug("Failed to read preview from %s", md_path, exc_info=True)
    return [], preview


def _format_file_entry(file: dict, lines: list[str]) -> None:
    """Append a single file entry to the message lines."""
    size_str = _format_size(int(file.get("size", 0)))
    virtual_path = file.get("path", f"{VIRTUAL_PATH_PREFIX}/uploads/{file['filename']}")
    lines.append(f"- {file['filename']} ({size_str})")
    lines.append(f"  Path: {virtual_path}")

    outline = file.get("outline") or []
    if outline:
        lines.append("  Document outline (use `file_read` with line ranges to read sections):")
        for entry in outline[:30]:
            lines.append(f"    L{entry.get('line', 0)}: {entry.get('title', '')}")
        if len(outline) > 30:
            lines.append(f"    ... ({len(outline) - 30} more headings)")
    else:
        preview = file.get("outline_preview") or []
        if preview:
            lines.append("  No structural headings detected. Document begins with:")
            for text in preview:
                lines.append(f"    > {text}")
        lines.append(
            "  Use `grep_tool` to search for keywords "
            f"(e.g. `grep_tool(pattern='keyword', path='{VIRTUAL_PATH_PREFIX}/uploads/')`)."
        )
    lines.append("")


def _build_uploaded_files_message(new_files: list[dict], historical_files: list[dict]) -> str:
    """Build the ``<uploaded_files>`` prompt block."""
    lines: list[str] = ["<uploaded_files>"]

    lines.append("The following files were uploaded in this message:")
    lines.append("")
    if new_files:
        for f in new_files:
            _format_file_entry(f, lines)
    else:
        lines.append("(empty)")
        lines.append("")

    if historical_files:
        lines.append("The following files were uploaded in previous messages and are still available:")
        lines.append("")
        for f in historical_files:
            _format_file_entry(f, lines)

    lines.append("To work with these files:")
    lines.append("- Read from the file first — use `file_read(path='/mnt/user-data/uploads/<filename>')`.")
    lines.append("- Use `grep_tool` to search for keywords when you are not sure which section to look at.")
    lines.append("- Use `glob_tool` to find files by name pattern (e.g. `**/*.md`).")
    lines.append("- Only fall back to web search if the file content is clearly insufficient.")
    lines.append("</uploaded_files>")

    return "\n".join(lines)


class UploadsMiddleware(HarnessAgentMiddleware):
    """Scan the thread uploads directory and inject file info into messages.

    The middleware looks for two sources of file metadata:

    1. ``additional_kwargs.files`` on the latest HumanMessage — files uploaded
       alongside the current user message.
    2. Files physically present in the thread's ``uploads/`` directory that are
       not listed in (1). These are "historical" uploads from earlier turns.

    A formatted ``<uploaded_files>`` block is prepended to the latest human
    message content. The original ``additional_kwargs`` is preserved.
    """

    name = "uploads"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    def _files_from_kwargs(self, message: HumanMessage, uploads_dir: Path | None) -> list[dict]:
        """Extract and validate file metadata from message.additional_kwargs.files."""
        kwargs_files = (message.additional_kwargs or {}).get("files")
        if not isinstance(kwargs_files, list) or not kwargs_files:
            return []

        files: list[dict] = []
        for f in kwargs_files:
            if not isinstance(f, dict):
                continue
            filename = f.get("filename") or ""
            if not filename or Path(filename).name != filename:
                continue
            if uploads_dir is not None and not (uploads_dir / filename).is_file():
                continue
            files.append(
                {
                    "filename": filename,
                    "size": int(f.get("size", 0)),
                    "path": f"{VIRTUAL_PATH_PREFIX}/uploads/{filename}",
                    "extension": Path(filename).suffix,
                }
            )
        return files

    def _list_historical_files(self, uploads_dir: Path, exclude_names: set[str]) -> list[dict]:
        """Scan uploads_dir for files not in the current message."""
        if not uploads_dir.exists():
            return []

        files: list[dict] = []
        try:
            entries = sorted(uploads_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return []

        for file_path in entries:
            if not file_path.is_file():
                continue
            if file_path.name in exclude_names:
                continue
            if file_path.suffix.lower() == ".md" and (uploads_dir / file_path.stem).exists():
                # Skip companion markdown of convertible originals
                continue

            stat = file_path.stat()
            outline, preview = _extract_outline_for_file(file_path)
            files.append(
                {
                    "filename": file_path.name,
                    "size": stat.st_size,
                    "path": f"{VIRTUAL_PATH_PREFIX}/uploads/{file_path.name}",
                    "extension": file_path.suffix,
                    "outline": outline,
                    "outline_preview": preview,
                }
            )
        return files

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        last_message = messages[-1]
        if not isinstance(last_message, HumanMessage):
            return None

        thread_id = state.get("thread_id", "default")
        user_id = state.get("user_id")
        uploads_dir = get_paths().sandbox_uploads_dir(thread_id, user_id=user_id)

        # Current message uploads
        new_files = self._files_from_kwargs(last_message, uploads_dir)

        # Historical uploads (everything else in the directory)
        new_filenames = {f["filename"] for f in new_files}
        historical_files = self._list_historical_files(uploads_dir, new_filenames)

        # Attach outlines to new files too
        for f in new_files:
            outline, preview = _extract_outline_for_file(uploads_dir / f["filename"])
            f["outline"] = outline
            f["outline_preview"] = preview

        if not new_files and not historical_files:
            return None

        logger.debug(
            "UploadsMiddleware injected new=%d historical=%d for thread=%s",
            len(new_files),
            len(historical_files),
            thread_id,
        )

        files_message = _build_uploaded_files_message(new_files, historical_files)

        # Prepend to the last human message content while preserving all metadata
        original_content = last_message.content
        if isinstance(original_content, str):
            updated_content = f"{files_message}\n\n{original_content}"
        elif isinstance(original_content, list):
            updated_content = [
                {"type": "text", "text": f"{files_message}\n\n"},
                *original_content,
            ]
        else:
            updated_content = original_content

        updated_message = HumanMessage(
            content=updated_content,
            id=last_message.id,
            name=last_message.name,
            additional_kwargs=last_message.additional_kwargs,
        )
        messages[-1] = updated_message

        return {
            "messages": messages,
            "uploaded_files": new_files,
        }
