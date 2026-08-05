"""ViewImageMiddleware — inject image data into messages before the model call.

Matches the harness design: runs at ``abefore_model``, scans the message
history for completed ``view_image`` tool results, and injects the image
content as a HumanMessage with multimodal content blocks so the vision model
can see the image.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import override

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

# MIME type map for common image extensions
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


class ViewImageMiddleware(HarnessAgentMiddleware):
    """Inject image data before the model call so vision models can see images.

    Scans for completed ``view_image`` tool calls in the message history and
    injects a HumanMessage with ``content=[{"type": "image_url", ...}]``
    before the model call.  Viewed images are cached in ``state["viewed_images"]``
    to avoid re-reading the same file across turns.
    """

    name = "view_image"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        # 与 view_image 工具上限 (harness/tools/builtins/view_image_tool.py) 保持一致
        self._max_image_bytes = 20 * 1024 * 1024  # 20 MiB

    # ------------------------------------------------------------------
    # image resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_image(path: str, max_bytes: int = 20 * 1024 * 1024) -> str | None:
        """Convert a local file path to a base64 data-URL, or return None."""
        if path.startswith(("http://", "https://", "data:")):
            return path

        p = Path(path)
        if not p.exists():
            logger.debug("view_image: file not found — %s", path)
            return None

        try:
            file_size = p.stat().st_size
        except OSError:
            return None

        if file_size > max_bytes:
            logger.warning("view_image: file too large — %s (%d bytes)", path, file_size)
            return None

        try:
            data = p.read_bytes()
            ext = p.suffix.lower()
            mime = _MIME_MAP.get(ext, "image/png")
            b64 = base64.b64encode(data).decode()
            return f"data:{mime};base64,{b64}"
        except Exception as exc:
            logger.warning("view_image: failed to encode %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # hook
    # ------------------------------------------------------------------

    @override
    async def abefore_model(self, state: HarnessState, runtime: Runtime) -> dict | None:
        """Inject image content from completed view_image tool calls."""
        messages = list(state.get("messages", []))
        viewed_images: dict[str, str] = dict(state.get("viewed_images", {}))

        # Find view_image ToolMessages that haven't been injected yet
        new_images: list[HumanMessage] = []
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            if msg.name != "view_image":
                continue

            image_path = str(msg.content).strip() if msg.content else ""
            if not image_path:
                continue

            # Already injected for this path?
            if image_path in viewed_images:
                continue

            resolved = self._resolve_image(image_path, self._max_image_bytes)
            if resolved is None:
                continue

            viewed_images[image_path] = resolved
            new_images.append(
                HumanMessage(
                    content=[
                        {"type": "text", "text": f"Image loaded: {image_path}"},
                        {"type": "image_url", "image_url": {"url": resolved}},
                    ],
                    additional_kwargs={"hide_from_ui": True},
                )
            )

        if not new_images:
            return None

        logger.debug("view_image: injecting %d image(s) before model call", len(new_images))
        return {
            "messages": new_images,
            "viewed_images": viewed_images,
        }
