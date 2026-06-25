"""Tests for UploadsMiddleware."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

# Use a temporary data root for all tests in this module.
@pytest.fixture(autouse=True)
def temp_data_root(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="harness_uploads_test_")
    monkeypatch.setenv("HARNESS_DATA_ROOT", tmp)
    # Reset paths singleton so it picks up the new env var.
    from harness.config.paths import set_paths, Paths

    set_paths(Paths())
    yield tmp


@pytest.fixture
def paths(temp_data_root):
    from harness.config.paths import get_paths

    return get_paths()


@pytest.fixture
def runtime():
    return MagicMock(spec=Runtime)


@pytest.mark.asyncio
async def test_injects_new_files_from_message_kwargs(paths, runtime):
    """Files listed in additional_kwargs.files should appear in the prompt."""
    from harness.middleware.uploads import UploadsMiddleware
    from harness.models import initial_state

    tid, uid = "thread-t1", "user-u1"
    paths.ensure_thread_dirs(tid, user_id=uid)
    (paths.sandbox_uploads_dir(tid, user_id=uid) / "report.txt").write_text(
        "hello", encoding="utf-8"
    )

    state = initial_state(
        tid,
        uid,
        "analyze this",
        files=[{"filename": "report.txt", "size": 5}],
    )
    mw = UploadsMiddleware()
    result = await mw.abefore_agent(state, runtime)

    assert result is not None
    assert len(result["messages"]) == 1
    content = result["messages"][0].content
    assert "<uploaded_files>" in content
    assert "report.txt" in content
    assert "/mnt/user-data/uploads/report.txt" in content
    assert result["uploaded_files"][0]["filename"] == "report.txt"


@pytest.mark.asyncio
async def test_includes_historical_files(paths, runtime):
    """Files already present in uploads dir but not in kwargs should also be listed."""
    from harness.middleware.uploads import UploadsMiddleware
    from harness.models import initial_state

    tid, uid = "thread-t2", "user-u2"
    paths.ensure_thread_dirs(tid, user_id=uid)
    uploads = paths.sandbox_uploads_dir(tid, user_id=uid)
    (uploads / "old.txt").write_text("old content", encoding="utf-8")
    (uploads / "new.txt").write_text("new content", encoding="utf-8")

    state = initial_state(
        tid,
        uid,
        "analyze",
        files=[{"filename": "new.txt", "size": 10}],
    )
    result = await UploadsMiddleware().abefore_agent(state, runtime)

    content = result["messages"][0].content
    assert "new.txt" in content
    assert "old.txt" in content
    # historical file should appear in the historical section
    assert "uploaded in previous messages" in content


@pytest.mark.asyncio
async def test_returns_none_when_no_files(paths, runtime):
    """Middleware should be a no-op when no files are present."""
    from harness.middleware.uploads import UploadsMiddleware
    from harness.models import initial_state

    tid, uid = "thread-t3", "user-u3"
    paths.ensure_thread_dirs(tid, user_id=uid)
    state = initial_state(tid, uid, "hello")
    result = await UploadsMiddleware().abefore_agent(state, runtime)
    assert result is None


@pytest.mark.asyncio
async def test_preserves_additional_kwargs(paths, runtime):
    """The original additional_kwargs (including files) must be preserved."""
    from harness.middleware.uploads import UploadsMiddleware
    from harness.models import initial_state

    tid, uid = "thread-t4", "user-u4"
    paths.ensure_thread_dirs(tid, user_id=uid)
    (paths.sandbox_uploads_dir(tid, user_id=uid) / "a.txt").write_text("a", encoding="utf-8")

    files = [{"filename": "a.txt", "size": 1}]
    state = initial_state(tid, uid, "hello", files=files)
    result = await UploadsMiddleware().abefore_agent(state, runtime)

    updated = result["messages"][0]
    assert isinstance(updated, HumanMessage)
    assert updated.additional_kwargs.get("files") == files


@pytest.mark.asyncio
async def test_skips_files_not_on_disk(paths, runtime):
    """Files in kwargs that do not exist physically should be ignored."""
    from harness.middleware.uploads import UploadsMiddleware
    from harness.models import initial_state

    tid, uid = "thread-t5", "user-u5"
    paths.ensure_thread_dirs(tid, user_id=uid)
    state = initial_state(
        tid,
        uid,
        "hello",
        files=[{"filename": "missing.txt", "size": 5}],
    )
    result = await UploadsMiddleware().abefore_agent(state, runtime)
    assert result is None
