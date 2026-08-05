"""Built-in ``view_image`` / ``list_uploaded_files`` tools — 上传文件查看与图片视觉输入.

``view_image`` 自身不返回图片数据, 而是把图片的宿主路径写进 ToolMessage;
``ViewImageMiddleware`` 在下一次模型调用前扫描该 ToolMessage, 读取图片并以
``image_url`` content block (base64 data-URL) 注入一条 HumanMessage — 这是
本项目的多模态消息格式 (LangChain ``image_url`` block, 由 ChatOpenAI 转为
OpenAI vision 输入). 因此二者必须配套使用: 工具负责路径校验, middleware
负责实际注入.

``list_uploaded_files`` 列出当前 thread uploads 目录的全部文件 (文件名/大小/
虚拟路径), 供 agent 在不依赖 prompt 注入的情况下主动发现上传文件.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from harness.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from harness.tools.sandbox_tools import _normalize_virtual_path

logger = logging.getLogger(__name__)

# 允许查看的图片扩展名 (svg 按文本处理, 不走视觉通道)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 与 ViewImageMiddleware._max_image_bytes 保持一致 — 超过上限 middleware 不会注入,
# 因此工具在更早的位置直接拒绝, 避免"看似成功实则模型看不到"的静默失败.
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB


def _state_ids(state: dict | None) -> tuple[str, str | None]:
    """从 graph state 提取 (thread_id, user_id)."""
    state = state or {}
    return state.get("thread_id") or "default", state.get("user_id")


def _format_size(size_bytes: int) -> str:
    """Human-readable file size (与 UploadsMiddleware 口径一致)."""
    kb = size_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def view_image_tool() -> BaseTool:
    """Create the ``view_image`` tool used by the Lead Agent."""

    @tool
    async def view_image(
        path: str,
        state: Annotated[dict, InjectedState] = None,
    ) -> str:
        """View an image file so you can see its visual content.

        Use this for images the user uploaded or that were generated during the
        task (charts, screenshots, diagrams). The image is loaded and shown to
        you before the next model call.

        Args:
            path: Virtual path of the image, e.g.
                ``/mnt/user-data/uploads/photo.png``. Relative paths are treated
                as workspace-relative. Supported formats: jpg, jpeg, png, webp,
                gif (max 20MB).
        """
        try:
            virtual_path = _normalize_virtual_path(path)
        except ValueError as exc:
            return f"[error] {exc}"

        ext = Path(virtual_path).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            allowed = ", ".join(sorted(IMAGE_EXTENSIONS))
            return f"[error] not a supported image format (allowed: {allowed}): {path}"

        thread_id, user_id = _state_ids(state)
        try:
            host_path = get_paths().resolve_virtual_path(
                thread_id, virtual_path, user_id=user_id
            )
        except ValueError as exc:
            return f"[error] {exc}"

        if not host_path.is_file():
            return f"[error] image not found: {path}"

        try:
            size = host_path.stat().st_size
        except OSError as exc:
            return f"[error] cannot read image: {exc}"
        if size > MAX_IMAGE_BYTES:
            return (
                f"[error] image too large ({_format_size(size)}, "
                f"max {_format_size(MAX_IMAGE_BYTES)}): {path}"
            )

        # 返回宿主路径 — ViewImageMiddleware 据此读取图片并注入 image_url block.
        # 注意: 返回值会进入 ToolMessage, 改动格式需同步 middleware 的解析逻辑.
        return str(host_path)

    return view_image


def list_uploaded_files_tool() -> BaseTool:
    """Create the ``list_uploaded_files`` tool used by the Lead Agent."""

    @tool
    async def list_uploaded_files(
        state: Annotated[dict, InjectedState] = None,
    ) -> str:
        """List all files the user has uploaded in this thread.

        Returns each file's name, size, and virtual path under
        ``/mnt/user-data/uploads/``. Use this when you are unsure which files
        are available, before reading or viewing them.
        """
        thread_id, user_id = _state_ids(state)
        uploads_dir = get_paths().sandbox_uploads_dir(thread_id, user_id=user_id)
        if not uploads_dir.is_dir():
            return "(no uploaded files)"

        lines: list[str] = []
        try:
            entries = sorted(uploads_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            return f"[error] cannot list uploads: {exc}"

        for p in entries:
            if not p.is_file():
                continue
            lines.append(
                f"- {p.name} ({_format_size(p.stat().st_size)})"
                f" — {VIRTUAL_PATH_PREFIX}/uploads/{p.name}"
            )
        return "\n".join(lines) or "(no uploaded files)"

    return list_uploaded_files
