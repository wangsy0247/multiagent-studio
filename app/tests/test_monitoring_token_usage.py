"""Tests for app /monitoring/token-usage (thread 归属校验 + 参数透传)."""
from __future__ import annotations

import uuid as uuid_mod
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
from app.api import monitoring as monitoring_api
from app.models.thread import Thread
from app.models.user import User

pytestmark = pytest.mark.asyncio


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


async def _make_thread(db, user: User) -> Thread:
    thread = Thread(id=uuid_mod.uuid4(), user_id=user.id, title="t")
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


def _mock_harness(monkeypatch):
    client = MagicMock()
    client.get_token_usage = AsyncMock(return_value={"total_tokens": 1})
    monkeypatch.setattr(monitoring_api, "get_harness_client", lambda: client)
    return client


async def test_thread_passthrough_and_params(db, monkeypatch):
    client = _mock_harness(monkeypatch)
    user = await _make_user(db, "alice")
    thread = await _make_thread(db, user)
    resp = await monitoring_api.get_token_usage(
        user_id=None, thread_id=str(thread.id), start_date="2026-01-01",
        end_date=None, model=None, current_user=user, db=db,
    )
    assert resp == {"total_tokens": 1}
    params = client.get_token_usage.await_args.kwargs
    assert params["user_id"] == "alice"
    assert params["thread_id"] == str(thread.id)
    assert params["start_date"] == "2026-01-01"


async def test_thread_ownership_rejected(db, monkeypatch):
    _mock_harness(monkeypatch)
    alice = await _make_user(db, "alice")
    bob = await _make_user(db, "bob")
    thread = await _make_thread(db, alice)
    with pytest.raises(HTTPException) as exc:
        await monitoring_api.get_token_usage(
            user_id=None, thread_id=str(thread.id), start_date=None,
            end_date=None, model=None, current_user=bob, db=db,
        )
    assert exc.value.status_code == 404


async def test_invalid_thread_id(db, monkeypatch):
    _mock_harness(monkeypatch)
    user = await _make_user(db, "alice")
    with pytest.raises(HTTPException) as exc:
        await monitoring_api.get_token_usage(
            user_id=None, thread_id="not-a-uuid", start_date=None,
            end_date=None, model=None, current_user=user, db=db,
        )
    assert exc.value.status_code == 400


async def test_no_thread_global_query(db, monkeypatch):
    client = _mock_harness(monkeypatch)
    user = await _make_user(db, "alice")
    await monitoring_api.get_token_usage(
        user_id=None, thread_id=None, start_date=None,
        end_date=None, model=None, current_user=user, db=db,
    )
    params = client.get_token_usage.await_args.kwargs
    assert "thread_id" not in params
    assert params["user_id"] == "alice"
