"""ViewImageMiddleware — convert view_image tool results into multimodal format."""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import override

from langchain_core.messages import ToolMessage

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)


class ViewImageMiddleware(HarnessAgentMiddleware):
    """Post-process ``view_image`` tool results so the frontend can render them.

    Uses ``awrap_tool_call`` to intercept view_image tool execution and
    convert local file paths to data-URLs or absolute URLs.
    """

    name = "view_image"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @override
    async def awrap_tool_call(self, request, handler):
        """Wrap view_image tool calls to resolve image paths."""
        tool_name = request.tool_call.get("name", "")

        # Execute the tool normally
        result = await handler(request)

        # Only post-process view_image results
        if tool_name != "view_image":
            return result

        image_path = str(result.content).strip() if hasattr(result, "content") else ""
        if image_path:
            resolved = self._resolve(image_path)
            additional_kwargs = dict(getattr(result, "additional_kwargs", {}) or {})
            additional_kwargs["image_url"] = resolved
            additional_kwargs["hide_from_ui"] = True
            result = result.model_copy(update={"additional_kwargs": additional_kwargs})

        return result

    @staticmethod
    def _resolve(path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        p = Path(path)
        if not p.exists():
            return f"file://{path}"
        if p.stat().st_size < 5 * 1024 * 1024:  # 5 MiB limit
            try:
                data = p.read_bytes()
                ext = p.suffix.lower()
                mime_map = {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp", ".svg": "image/svg+xml",
                }
                mime = mime_map.get(ext, "image/png")
                b64 = base64.b64encode(data).decode()
                return f"data:{mime};base64,{b64}"
            except Exception as exc:
                logger.warning("Failed to encode image %s: %s", path, exc)
        return f"file://{path}"
