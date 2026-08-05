"""present_files 工具测试（产物出口闭环）."""
from __future__ import annotations

import pytest

from harness.tools.builtins.lead_tools import build_lead_tools
from harness.tools.builtins.present_files_tool import (
    OUTPUTS_VIRTUAL_PREFIX,
    _normalize_presented_filepath,
    present_files_tool,
)


# ── 路径规范化与校验 ─────────────────────────────────────────


def test_normalize_accepts_outputs_path():
    assert (
        _normalize_presented_filepath("/mnt/user-data/outputs/report.md")
        == "/mnt/user-data/outputs/report.md"
    )


def test_normalize_collapses_dot_segments():
    assert (
        _normalize_presented_filepath("/mnt/user-data/outputs/./sub//a.txt")
        == "/mnt/user-data/outputs/sub/a.txt"
    )


def test_normalize_rejects_outside_outputs():
    for bad in (
        "/mnt/user-data/uploads/x.txt",
        "/mnt/user-data/workspace/x.txt",
        "/etc/passwd",
        "report.md",
        "/mnt/user-data/outputs",  # 裸目录, 不是文件
    ):
        with pytest.raises(ValueError):
            _normalize_presented_filepath(bad)


def test_normalize_rejects_traversal():
    with pytest.raises(ValueError, match="Only files in"):
        _normalize_presented_filepath("/mnt/user-data/outputs/../uploads/x.txt")
    with pytest.raises(ValueError, match="Only files in"):
        _normalize_presented_filepath("/mnt/user-data/outputs/../../etc/passwd")


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        _normalize_presented_filepath("   ")


# ── 工具行为 (Command 更新) ──────────────────────────────────


@pytest.mark.asyncio
async def test_present_files_writes_artifacts_command():
    tool = present_files_tool()
    cmd = await tool.coroutine(
        filepaths=["/mnt/user-data/outputs/report.md", "/mnt/user-data/outputs/chart.png"],
        tool_call_id="tc-1",
    )
    assert cmd.update["artifacts"] == [
        "/mnt/user-data/outputs/report.md",
        "/mnt/user-data/outputs/chart.png",
    ]
    msgs = cmd.update["messages"]
    assert len(msgs) == 1 and msgs[0].tool_call_id == "tc-1"
    assert "Successfully presented" in msgs[0].content


@pytest.mark.asyncio
async def test_present_files_normalizes_paths():
    tool = present_files_tool()
    cmd = await tool.coroutine(
        filepaths=["/mnt/user-data/outputs/./report.md"],
        tool_call_id="tc-2",
    )
    assert cmd.update["artifacts"] == ["/mnt/user-data/outputs/report.md"]


@pytest.mark.asyncio
async def test_present_files_rejects_non_outputs_path():
    """路径非法时返回明确错误 ToolMessage, 且不写 artifacts."""
    tool = present_files_tool()
    cmd = await tool.coroutine(
        filepaths=["/mnt/user-data/workspace/draft.md"],
        tool_call_id="tc-3",
    )
    assert "artifacts" not in cmd.update
    content = cmd.update["messages"][0].content
    assert content.startswith("Error:")
    assert OUTPUTS_VIRTUAL_PREFIX in content
    assert "/mnt/user-data/workspace/draft.md" in content


@pytest.mark.asyncio
async def test_present_files_rejects_traversal():
    tool = present_files_tool()
    cmd = await tool.coroutine(
        filepaths=["/mnt/user-data/outputs/../uploads/secret.txt"],
        tool_call_id="tc-4",
    )
    assert "artifacts" not in cmd.update
    assert "Error:" in cmd.update["messages"][0].content


# ── 注册与 reducer ───────────────────────────────────────────


def test_build_lead_tools_includes_present_files():
    tools = build_lead_tools(None)
    assert "present_files" in [t.name for t in tools]


def test_merge_artifacts_merges_and_dedupes():
    from harness.models import merge_artifacts

    assert merge_artifacts(["a"], ["b", "a", "c"]) == ["a", "b", "c"]
    assert merge_artifacts(["a"], []) == ["a"]
