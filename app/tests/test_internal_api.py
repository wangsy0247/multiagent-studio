"""内部服务间 API 测试（Agent 自建定时任务入口）"""

from datetime import datetime, timedelta, timezone

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
from app.api import internal
from app.models.scheduled_task import ScheduledTask
from app.models.user import User


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_user(db) -> User:
    user = User(email="a@b.com", username="tester", hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── 服务间认证 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_auth_disabled_without_env(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        await internal.require_internal_token(x_internal_token="whatever")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_token_auth_wrong_token(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "secret")
    with pytest.raises(HTTPException) as exc:
        await internal.require_internal_token(x_internal_token="wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        await internal.require_internal_token(x_internal_token=None)


@pytest.mark.asyncio
async def test_token_auth_ok(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "secret")
    await internal.require_internal_token(x_internal_token="secret")  # 不抛异常即通过


# ── Agent 创建任务 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_marks_created_by_agent(db):
    user = await _make_user(db)
    req = internal.InternalTaskCreate(
        username="tester",
        name="Agent 创建的任务",
        prompt="每天汇总邮件",
        cron_expr="0 9 * * *",
    )
    task = await internal.create_task_internal(req, db=db)
    assert task.created_by == "agent"
    assert task.user_id == user.id
    assert task.enabled is True
    assert task.next_run_at is not None


@pytest.mark.asyncio
async def test_create_task_unknown_user_404(db):
    req = internal.InternalTaskCreate(
        username="ghost", name="x", prompt="y", cron_expr="0 9 * * *",
    )
    with pytest.raises(HTTPException) as exc:
        await internal.create_task_internal(req, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_internal_list_scoped_to_user(db):
    user = await _make_user(db)
    other = User(email="o@b.com", username="other", hashed_password="x")
    db.add(other)
    await db.commit()
    await db.refresh(other)
    db.add(ScheduledTask(
        user_id=user.id, name="mine", prompt="x",
        cron_expr="0 9 * * *", timezone="UTC",
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        created_by="agent",
    ))
    db.add(ScheduledTask(
        user_id=other.id, name="not-mine", prompt="x",
        cron_expr="0 9 * * *", timezone="UTC",
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    ))
    await db.commit()

    tasks = await internal.list_tasks_internal(username="tester", db=db)
    assert len(tasks) == 1
    assert tasks[0].name == "mine"


@pytest.mark.asyncio
async def test_internal_pause_resume_via_update(db):
    user = await _make_user(db)
    task = ScheduledTask(
        user_id=user.id, name="t", prompt="x",
        cron_expr="0 9 * * *", timezone="UTC",
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        created_by="agent", enabled=True,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    from app.api.scheduled_tasks import ScheduledTaskUpdate
    paused = await internal.update_task_internal(
        task.id, ScheduledTaskUpdate(enabled=False), username="tester", db=db
    )
    assert paused.enabled is False
    resumed = await internal.update_task_internal(
        task.id, ScheduledTaskUpdate(enabled=True), username="tester", db=db
    )
    assert resumed.enabled is True
    # resume 时 next_run_at 已过期会被重算到未来
    assert resumed.next_run_at is not None
