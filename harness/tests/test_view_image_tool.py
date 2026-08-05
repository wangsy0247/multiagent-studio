"""view_image / list_uploaded_files 工具测试 (含 ViewImageMiddleware 注入链路)."""
from __future__ import annotations

import base64
import tempfile
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from harness.tools.builtins.view_image_tool import (
    MAX_IMAGE_BYTES,
    list_uploaded_files_tool,
    view_image_tool,
)

# 1x1 透明 PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def temp_data_root(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="harness_view_image_test_")
    monkeypatch.setenv("HARNESS_DATA_ROOT", tmp)
    from harness.config.paths import Paths, set_paths

    set_paths(Paths())
    yield tmp


@pytest.fixture
def paths(temp_data_root):
    from harness.config.paths import get_paths

    return get_paths()


@pytest.fixture
def runtime():
    return MagicMock(spec=Runtime)


def _state(tid: str, uid: str) -> dict:
    return {"thread_id": tid, "user_id": uid}


# ── view_image 工具 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_image_returns_host_path(paths):
    tid, uid = "thread-v1", "user-v1"
    paths.ensure_thread_dirs(tid, user_id=uid)
    img = paths.sandbox_uploads_dir(tid, user_id=uid) / "photo.png"
    img.write_bytes(_PNG_BYTES)

    tool = view_image_tool()
    result = await tool.coroutine(
        path="/mnt/user-data/uploads/photo.png", state=_state(tid, uid)
    )
    assert result == str(img)


@pytest.mark.asyncio
async def test_view_image_accepts_workspace_relative_path(paths):
    tid, uid = "thread-v2", "user-v2"
    paths.ensure_thread_dirs(tid, user_id=uid)
    img = paths.sandbox_work_dir(tid, user_id=uid) / "chart.jpg"
    img.write_bytes(_PNG_BYTES)

    tool = view_image_tool()
    result = await tool.coroutine(path="chart.jpg", state=_state(tid, uid))
    assert result == str(img)


@pytest.mark.asyncio
async def test_view_image_rejects_non_image(paths):
    tid, uid = "thread-v3", "user-v3"
    paths.ensure_thread_dirs(tid, user_id=uid)
    (paths.sandbox_uploads_dir(tid, user_id=uid) / "notes.txt").write_text("hi")

    tool = view_image_tool()
    result = await tool.coroutine(
        path="/mnt/user-data/uploads/notes.txt", state=_state(tid, uid)
    )
    assert result.startswith("[error]")
    assert "not a supported image format" in result


@pytest.mark.asyncio
async def test_view_image_rejects_missing_file(paths):
    tid, uid = "thread-v4", "user-v4"
    paths.ensure_thread_dirs(tid, user_id=uid)

    tool = view_image_tool()
    result = await tool.coroutine(
        path="/mnt/user-data/uploads/missing.png", state=_state(tid, uid)
    )
    assert result.startswith("[error]")
    assert "not found" in result


@pytest.mark.asyncio
async def test_view_image_rejects_oversized(paths):
    tid, uid = "thread-v5", "user-v5"
    paths.ensure_thread_dirs(tid, user_id=uid)
    big = paths.sandbox_uploads_dir(tid, user_id=uid) / "big.png"
    with big.open("wb") as f:
        f.truncate(MAX_IMAGE_BYTES + 1)

    tool = view_image_tool()
    result = await tool.coroutine(
        path="/mnt/user-data/uploads/big.png", state=_state(tid, uid)
    )
    assert result.startswith("[error]")
    assert "too large" in result


@pytest.mark.asyncio
async def test_view_image_rejects_host_absolute_path(paths):
    tool = view_image_tool()
    result = await tool.coroutine(path="/etc/hostname.png", state=_state("t", "u"))
    assert result.startswith("[error]")


@pytest.mark.asyncio
async def test_view_image_rejects_traversal(paths):
    tid, uid = "thread-v6", "user-v6"
    paths.ensure_thread_dirs(tid, user_id=uid)
    tool = view_image_tool()
    result = await tool.coroutine(
        path="/mnt/user-data/uploads/../../etc/secret.png", state=_state(tid, uid)
    )
    assert result.startswith("[error]")


# ── ViewImageMiddleware 注入链路 ─────────────────────────────


@pytest.mark.asyncio
async def test_middleware_injects_image_for_view_image_result(paths, runtime):
    """工具返回宿主路径后, middleware 应以 image_url block 注入图片."""
    from harness.middleware.view_image import ViewImageMiddleware

    tid, uid = "thread-v7", "user-v7"
    paths.ensure_thread_dirs(tid, user_id=uid)
    img = paths.sandbox_uploads_dir(tid, user_id=uid) / "photo.png"
    img.write_bytes(_PNG_BYTES)

    state = {
        "messages": [
            HumanMessage(content="look at this"),
            ToolMessage(content=str(img), name="view_image", tool_call_id="tc-1"),
        ],
        "thread_id": tid,
        "user_id": uid,
    }
    result = await ViewImageMiddleware().abefore_model(state, runtime)

    assert result is not None
    injected = result["messages"][0]
    assert isinstance(injected, HumanMessage)
    assert isinstance(injected.content, list)
    block_types = [b.get("type") for b in injected.content]
    assert "image_url" in block_types
    image_block = next(b for b in injected.content if b.get("type") == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
    assert str(img) in result["viewed_images"]


@pytest.mark.asyncio
async def test_middleware_does_not_reinject_cached_image(paths, runtime):
    from harness.middleware.view_image import ViewImageMiddleware

    tid, uid = "thread-v8", "user-v8"
    paths.ensure_thread_dirs(tid, user_id=uid)
    img = paths.sandbox_uploads_dir(tid, user_id=uid) / "photo.png"
    img.write_bytes(_PNG_BYTES)

    state = {
        "messages": [
            ToolMessage(content=str(img), name="view_image", tool_call_id="tc-1"),
        ],
        "thread_id": tid,
        "user_id": uid,
        "viewed_images": {str(img): "data:image/png;base64,cached"},
    }
    result = await ViewImageMiddleware().abefore_model(state, runtime)
    assert result is None


# ── list_uploaded_files 工具 ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_uploaded_files_empty(paths):
    tid, uid = "thread-l1", "user-l1"
    paths.ensure_thread_dirs(tid, user_id=uid)

    tool = list_uploaded_files_tool()
    result = await tool.coroutine(state=_state(tid, uid))
    assert result == "(no uploaded files)"


@pytest.mark.asyncio
async def test_list_uploaded_files_lists_name_size_path(paths):
    tid, uid = "thread-l2", "user-l2"
    paths.ensure_thread_dirs(tid, user_id=uid)
    uploads = paths.sandbox_uploads_dir(tid, user_id=uid)
    (uploads / "a.txt").write_text("x" * 2048, encoding="utf-8")
    (uploads / "b.png").write_bytes(_PNG_BYTES)

    tool = list_uploaded_files_tool()
    result = await tool.coroutine(state=_state(tid, uid))

    lines = result.splitlines()
    assert len(lines) == 2
    assert "a.txt (2.0 KB)" in lines[0]
    assert "/mnt/user-data/uploads/a.txt" in lines[0]
    assert "b.png" in lines[1]
    assert "/mnt/user-data/uploads/b.png" in lines[1]


# ── 注册 ─────────────────────────────────────────────────────


def test_build_lead_tools_includes_view_image_and_list_uploaded_files():
    from harness.tools.builtins.lead_tools import build_lead_tools

    names = [t.name for t in build_lead_tools(None)]
    assert "view_image" in names
    assert "list_uploaded_files" in names
