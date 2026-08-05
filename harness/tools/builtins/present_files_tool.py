"""Built-in ``present_files`` tool — 把交付文件呈现给用户（产物出口闭环）.

对齐 DeerFlow ``present_file_tool``: agent 在把交付物保存到
``/mnt/user-data/outputs`` 后调用本工具，规范化后的虚拟路径经
``Command(update={"artifacts": ...})`` 写入 graph state（``merge_artifacts``
reducer 负责合并去重），前端按 tool_call 事件的 tool_name 归组渲染文件卡片。
"""

from __future__ import annotations

import posixpath
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.types import Command

from harness.config.paths import VIRTUAL_PATH_PREFIX

OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def _normalize_presented_filepath(filepath: str) -> str:
    """规范化呈现路径，强制约束在 ``/mnt/user-data/outputs/`` 内.

    只接受虚拟沙箱路径（如 ``/mnt/user-data/outputs/report.md``）。
    ``posixpath.normpath`` 会折叠 ``.``/``..``，因此穿越到 outputs 之外的
    路径（如 ``/mnt/user-data/outputs/../uploads/x``）会在前缀校验处被拒绝。

    Raises:
        ValueError: 路径为空或不在 outputs 目录内。
    """
    if not isinstance(filepath, str) or not filepath.strip():
        raise ValueError("Empty file path")
    normalized = posixpath.normpath(filepath.strip().replace("\\", "/"))
    if not normalized.startswith(OUTPUTS_VIRTUAL_PREFIX + "/"):
        raise ValueError(
            f"Only files in {OUTPUTS_VIRTUAL_PREFIX} can be presented: {filepath}"
        )
    return normalized


def present_files_tool() -> BaseTool:
    """Create the ``present_files`` tool used by the Lead Agent."""

    @tool("present_files", parse_docstring=True)
    async def present_files(
        filepaths: list[str],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Make files visible to the user for viewing and downloading in the client interface.

        When to use the present_files tool:

        - Making any file available for the user to view, download, or interact with
        - Presenting multiple related files at once
        - After creating files that should be presented to the user

        When NOT to use the present_files tool:
        - When you only need to read file contents for your own processing
        - For temporary or intermediate files not meant for user viewing

        Notes:
        - You MUST call this tool after saving final deliverables to the `/mnt/user-data/outputs` directory.
        - This tool can be safely called in parallel with other tools. State updates are handled by a reducer to prevent conflicts.

        Args:
            filepaths: List of absolute file paths to present to the user. **Only** files in `/mnt/user-data/outputs` can be presented.
        """
        try:
            normalized_paths = [
                _normalize_presented_filepath(fp) for fp in filepaths
            ]
        except ValueError as exc:
            return Command(
                update={
                    "messages": [
                        ToolMessage(f"Error: {exc}", tool_call_id=tool_call_id)
                    ]
                },
            )

        # merge_artifacts reducer 负责合并去重
        return Command(
            update={
                "artifacts": normalized_paths,
                "messages": [
                    ToolMessage(
                        "Successfully presented files", tool_call_id=tool_call_id
                    )
                ],
            },
        )

    return present_files
