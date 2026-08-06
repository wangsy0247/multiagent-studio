"""P0 安全修复回归测试: projects/agents 鉴权、路径消毒、stop 归属校验."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api import agents as agents_api
from app.api import execute as execute_api
from app.api import projects as projects_api
from app.models.thread import Thread
from app.models.user import User


@pytest.fixture(autouse=True)
def temp_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_user(db, username: str) -> User:
    user = User(email=f"{username}@b.com", username=username, hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _proj_dir(username: str, project_id: str) -> Path:
    from harness.config.paths import get_paths

    return get_paths().base_dir / "users" / username / "projects" / project_id


async def _create_project(username: str, project_id: str = "proj1") -> None:
    d = _proj_dir(username, project_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(json.dumps({"id": project_id, "name": "P"}))


# ── projects: 跨用户隔离 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_projects_scoped_to_current_user(db, temp_data_root):
    alice = await _make_user(db, "alice")
    bob = await _make_user(db, "bob")
    await _create_project("alice")

    # bob 看不到 alice 的项目
    result = await projects_api.list_projects(db, bob)
    assert result["count"] == 0

    # bob get alice 的项目 → 404
    with pytest.raises(HTTPException) as exc:
        await projects_api.get_project("proj1", db, bob)
    assert exc.value.status_code == 404

    # bob delete alice 的项目 → 404, 且目录仍在
    with pytest.raises(HTTPException) as exc:
        await projects_api.delete_project("proj1", db, bob)
    assert exc.value.status_code == 404
    assert _proj_dir("alice", "proj1").exists()

    # alice 自己正常
    result = await projects_api.list_projects(db, alice)
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_delete_project_success_not_404(db, temp_data_root):
    alice = await _make_user(db, "alice")
    await _create_project("alice")
    result = await projects_api.delete_project("proj1", db, alice)
    assert result["status"] == "deleted"
    assert not _proj_dir("alice", "proj1").exists()


# ── 路径消毒 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_traversal_rejected(db, temp_data_root):
    alice = await _make_user(db, "alice")
    with pytest.raises(HTTPException) as exc:
        await projects_api.get_project("../../../etc", db, alice)
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await projects_api.delete_project("..%2F..%2Fhome".replace("%2F", "/"), db, alice)
    assert exc.value.status_code == 400


# ── agents: 跨用户隔离 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_agents_scoped_to_current_user(db, temp_data_root):
    alice = await _make_user(db, "alice")
    bob = await _make_user(db, "bob")

    from harness.config.agents_config import AgentConfig, save_agent_config

    save_agent_config("coder", AgentConfig(name="coder", model="m", system_prompt="s"), user_id="alice")

    # bob 看不到 alice 的 agent
    result = await agents_api.list_agents(db, bob)
    assert result["count"] == 0
    with pytest.raises(HTTPException) as exc:
        await agents_api.get_agent("coder", db, bob)
    assert exc.value.status_code == 404

    # alice 能看到
    result = await agents_api.list_agents(db, alice)
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_global_config_write_only_own_dir(db, temp_data_root):
    alice = await _make_user(db, "alice")
    bob = await _make_user(db, "bob")

    class _Req:
        async def json(self):
            return {"config": {"default_model": "gpt-x"}}

    await agents_api.update_user_global_config(_Req(), db, alice)

    from harness.config.paths import get_paths

    alice_cfg = get_paths().base_dir / "users" / "alice" / "config.yaml"
    bob_cfg = get_paths().base_dir / "users" / "bob" / "config.yaml"
    assert alice_cfg.exists()
    assert not bob_cfg.exists()


# ── execute: stop 先校验归属 ─────────────────────────────────


@pytest.mark.asyncio
async def test_stop_other_users_thread_blocked(db, temp_data_root, monkeypatch):
    alice = await _make_user(db, "alice")
    bob = await _make_user(db, "bob")
    thread = Thread(user_id=alice.id, title="t")
    db.add(thread)
    await db.commit()
    await db.refresh(thread)

    harness = MagicMock()
    harness.stop_execution = AsyncMock(return_value={"status": "stopped"})
    monkeypatch.setattr(execute_api, "get_harness_client", lambda: harness)

    # bob 停 alice 的会话 → 404, harness 未被调用
    with pytest.raises(HTTPException) as exc:
        await execute_api.stop_execution(str(thread.id), bob, db)
    assert exc.value.status_code == 404
    harness.stop_execution.assert_not_called()

    # alice 停自己的 → 正常
    result = await execute_api.stop_execution(str(thread.id), alice, db)
    assert result["status"] == "stopped"
    harness.stop_execution.assert_called_once()


@pytest.mark.asyncio
async def test_stop_invalid_thread_id_400(db, temp_data_root):
    alice = await _make_user(db, "alice")
    with pytest.raises(HTTPException) as exc:
        await execute_api.stop_execution("not-a-uuid", alice, db)
    assert exc.value.status_code == 400
