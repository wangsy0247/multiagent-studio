"""UploadsMiddleware — scan and inject uploaded file context."""
from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class UploadsMiddleware(HarnessAgentMiddleware):
    """Scan the workspace uploads directory and inject file info into messages.

    Scanned files are described in a ``<uploaded_files>`` block that the agent
    can use as context.
    """

    name = "uploads"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        workspace = state.get("workspace", ".")
        uploads_dir = Path(workspace) / "uploads"
        if not uploads_dir.exists():
            return None

        files = [f for f in uploads_dir.iterdir() if f.is_file()]
        if not files:
            return None

        descriptions: list[str] = []
        for f in files:
            stat = f.stat()
            descriptions.append(
                f"- **{f.name}**  ({f.suffix}, {stat.st_size:,} bytes, path: {f})"
            )

        upload_message = SystemMessage(
            content="<uploaded_files>\n"
            + "\n".join(descriptions)
            + "\n</uploaded_files>\n"
            + "以上文件已上传到工作区，你可以使用 file_read 工具读取其内容。"
        )

        messages = list(state.get("messages", []))
        messages.insert(0, upload_message)

        logger.debug("UploadsMiddleware injected %d file descriptions", len(files))
        return {"messages": messages}
