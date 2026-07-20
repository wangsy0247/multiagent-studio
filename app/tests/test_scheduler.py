"""SchedulerService tick 认领逻辑测试（内存 SQLite，monkeypatch 掉真实 DB session）"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401 — 确保所有表进 metadata
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api import scheduled_tasks as st_api
from app.models.message import Message
from app.models.scheduled_task import ScheduledTask, TaskRun
from app.models.thread import Thread
from app.models.user import User
from app.services import scheduler as sched


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def db_maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(sched, "async_session", maker)
    yield maker
    await engine.dispose()


async def _make_user(db) -> User:
    user = User(email="t@example.com", username="tester", hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _capture_dispatch(monkeypatch) -> list:
    dispatched = []
    monkeypatch.setattr(
        sched.SchedulerService, "_dispatch",
        lambda self, task_id: dispatched.append(task_id),
    )
    return dispatched


@pytest.mark.asyncio
async def test_tick_claims_and_dispatches(db_maker, monkeypatch):
    """到期任务被认领：推进 next_run_at 后派发执行"""
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="每分钟", prompt="hello",
            cron_expr="* * * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) - timedelta(minutes=1)),
            enabled=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id, old_next = task.id, task.next_run_at

    service = sched.SchedulerService()
    await service._tick()

    assert dispatched == [task_id]
    async with db_maker() as db:
        t = await db.get(ScheduledTask, task_id)
        assert t.next_run_at > old_next  # 执行前已推进（at-most-once）


@pytest.mark.asyncio
async def test_tick_ignores_not_due(db_maker, monkeypatch):
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        db.add(ScheduledTask(
            user_id=user.id, name="未来", prompt="x",
            cron_expr="0 9 * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) + timedelta(hours=1)),
            enabled=True,
        ))
        await db.commit()

    service = sched.SchedulerService()
    await service._tick()
    assert dispatched == []


@pytest.mark.asyncio
async def test_tick_ignores_disabled(db_maker, monkeypatch):
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        db.add(ScheduledTask(
            user_id=user.id, name="禁用", prompt="x",
            cron_expr="* * * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) - timedelta(minutes=1)),
            enabled=False,
        ))
        await db.commit()

    service = sched.SchedulerService()
    await service._tick()
    assert dispatched == []


@pytest.mark.asyncio
async def test_tick_defers_when_fixed_thread_busy(db_maker, monkeypatch):
    """fixed 策略绑定的 thread 正在运行 → 本轮跳过，next_run_at 不变"""
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        thread = Thread(user_id=user.id, status="running")
        db.add(thread)
        await db.commit()
        await db.refresh(thread)
        task = ScheduledTask(
            user_id=user.id, name="固定会话", prompt="x",
            cron_expr="* * * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) - timedelta(minutes=1)),
            enabled=True, thread_strategy="fixed", thread_id=thread.id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id, old_next = task.id, task.next_run_at

    service = sched.SchedulerService()
    await service._tick()

    assert dispatched == []
    async with db_maker() as db:
        t = await db.get(ScheduledTask, task_id)
        assert t.next_run_at == old_next  # 未认领未推进


@pytest.mark.asyncio
async def test_tick_misfire_fast_forward(db_maker, monkeypatch):
    """停机过久的 recurring 任务：跳过本次，fast-forward，不补跑"""
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="每小时", prompt="x",
            cron_expr="0 * * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) - timedelta(hours=3)),
            enabled=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    service = sched.SchedulerService()
    await service._tick()

    assert dispatched == []
    async with db_maker() as db:
        t = await db.get(ScheduledTask, task_id)
        assert t.last_status == "skipped"
        assert t.enabled is True
        assert _naive(datetime.now(timezone.utc)) < t.next_run_at  # 快进到未来


@pytest.mark.asyncio
async def test_recover_interrupted_runs(db_maker):
    """启动恢复：running 状态的 run 标记为 interrupted"""
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="t", prompt="x",
            cron_expr="0 9 * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) + timedelta(hours=1)),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        run = TaskRun(task_id=task.id, status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    service = sched.SchedulerService()
    await service._recover_interrupted_runs()

    async with db_maker() as db:
        r = await db.get(TaskRun, run_id)
        assert r.status == "interrupted"
        assert r.finished_at is not None


class _FakeHarness:
    """模拟 harness 的 SSE 流式响应"""

    async def stream_execute(self, **kwargs):
        yield json.dumps({"type": "message", "content": "定时任务输出"})
        yield json.dumps({"type": "finished"})


class _SilentHarness:
    """模拟 Agent 回复 [SILENT] 的流"""

    async def stream_execute(self, **kwargs):
        _SilentHarness.last_message = kwargs.get("message", "")
        yield json.dumps({"type": "message", "content": "[SILENT]"})
        yield json.dumps({"type": "finished"})


class _HangingHarness:
    """模拟挂起的执行：发一个事件后永久沉默"""

    async def stream_execute(self, **kwargs):
        yield json.dumps({"type": "tool_call", "tool_name": "bash", "content": "bash ls"})
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_run_scheduled_task_executes_disabled_oneshot(db_maker, monkeypatch):
    """回归：一次性任务认领时已被置为 disabled，执行器不得因此拒绝执行

    （生产 bug：认领 fire-then-disable → 执行器 enabled 闸门 → 静默跳过，无执行记录）
    """
    monkeypatch.setattr(sched, "get_harness_client", lambda: _FakeHarness())
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="一次性", prompt="生成日报",
            recurring=False, timezone="Asia/Shanghai",
            next_run_at=_naive(datetime.now(timezone.utc) - timedelta(seconds=30)),
            enabled=False,  # 认领后状态：fire-then-disable
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await sched.run_scheduled_task(task_id)

    async with db_maker() as db:
        runs = (await db.execute(select(TaskRun).where(TaskRun.task_id == task_id))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].summary == "定时任务输出"
        assert runs[0].thread_id is not None
        # 消息已持久化到会话
        msgs = (await db.execute(
            select(Message).where(Message.thread_id == runs[0].thread_id)
        )).scalars().all()
        assert any(m.role == "ai" and m.content == "定时任务输出" for m in msgs)
        t = await db.get(ScheduledTask, task_id)
        assert t.last_status == "success"


@pytest.mark.asyncio
async def test_update_task_with_run_at_switches_to_oneshot(db_maker):
    """回归：PATCH 带 run_at 不应 500（run_at 不是模型字段）且能切换为一次性任务"""
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="周期", prompt="x",
            cron_expr="0 9 * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) + timedelta(hours=1)),
            enabled=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

        future = datetime.now(timezone.utc) + timedelta(hours=3)
        result = await st_api.update_task(
            task_id, st_api.ScheduledTaskUpdate(run_at=future), user, db
        )
        assert result.recurring is False
        assert result.cron_expr is None
        assert result.next_run_at is not None


@pytest.mark.asyncio
async def test_update_task_basic_fields(db_maker):
    """回归：仅修改名称/指令等基础字段可正常保存"""
    async with db_maker() as db:
        user = await _make_user(db)
        task = ScheduledTask(
            user_id=user.id, name="旧名", prompt="旧指令",
            cron_expr="0 9 * * *", recurring=True, timezone="UTC",
            next_run_at=_naive(datetime.now(timezone.utc) + timedelta(hours=1)),
            enabled=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        old_next = task.next_run_at

        result = await st_api.update_task(
            task.id,
            st_api.ScheduledTaskUpdate(name="新名", prompt="新指令", expires_at=None),
            user, db,
        )
        assert result.name == "新名"
        assert result.prompt == "新指令"
        assert result.next_run_at == old_next  # 调度字段未变，next_run_at 不被重置


async def _make_task(db, user, **overrides) -> ScheduledTask:
    defaults = dict(
        user_id=user.id, name="t", prompt="巡检",
        cron_expr="* * * * *", recurring=True, timezone="UTC",
        next_run_at=_naive(datetime.now(timezone.utc) - timedelta(seconds=30)),
        enabled=True,
    )
    defaults.update(overrides)
    task = ScheduledTask(**defaults)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@pytest.mark.asyncio
async def test_silent_run_writes_nothing_and_no_unread(db_maker, monkeypatch):
    """静默模式：Agent 回 [SILENT] → 不写会话消息、run 直接已读、prompt 注入了静默提示"""
    monkeypatch.setattr(sched, "get_harness_client", lambda: _SilentHarness())
    async with db_maker() as db:
        user = await _make_user(db)
        task = await _make_task(db, user, allow_silent=True)

        await sched.run_scheduled_task(task.id)

        runs = (await db.execute(select(TaskRun).where(TaskRun.task_id == task.id))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].summary is None
        assert runs[0].seen is True  # 静默不产生未读
        msgs = (await db.execute(
            select(Message).where(Message.thread_id == runs[0].thread_id)
        )).scalars().all()
        assert msgs == []  # 会话里没有写入任何消息
        t = await db.get(ScheduledTask, task.id)
        await db.refresh(t)  # 同 session 的 identity map 存的是创建时的旧实例
        assert t.last_status == "success"
        # prompt 注入了 [SILENT] 约定提示
        assert "[SILENT]" in _SilentHarness.last_message


@pytest.mark.asyncio
async def test_allow_silent_with_content_persists_and_unread(db_maker, monkeypatch):
    """静默模式但有实际输出：消息正常落库，run 未读（seen=False）"""
    monkeypatch.setattr(sched, "get_harness_client", lambda: _FakeHarness())
    async with db_maker() as db:
        user = await _make_user(db)
        task = await _make_task(db, user, allow_silent=True)

        await sched.run_scheduled_task(task.id)

        runs = (await db.execute(select(TaskRun).where(TaskRun.task_id == task.id))).scalars().all()
        assert runs[0].status == "success"
        assert runs[0].summary == "定时任务输出"
        assert runs[0].seen is False
        msgs = (await db.execute(
            select(Message).where(Message.thread_id == runs[0].thread_id)
        )).scalars().all()
        assert any(m.role == "ai" and m.content == "定时任务输出" for m in msgs)


@pytest.mark.asyncio
async def test_inactivity_timeout(db_maker, monkeypatch):
    """inactivity 超时：流长时间无事件 → run 标记 timeout"""
    monkeypatch.setattr(sched, "get_harness_client", lambda: _HangingHarness())
    monkeypatch.setattr(sched, "INACTIVITY_TIMEOUT", 0.05)
    async with db_maker() as db:
        user = await _make_user(db)
        task = await _make_task(db, user)

        await sched.run_scheduled_task(task.id)

        runs = (await db.execute(select(TaskRun).where(TaskRun.task_id == task.id))).scalars().all()
        assert runs[0].status == "timeout"
        assert "无" in runs[0].error and "活动" in runs[0].error


@pytest.mark.asyncio
async def test_unread_count_and_mark_seen(db_maker):
    """unread-count 按用户统计（排除 running 与已读），mark-seen 清除"""
    async with db_maker() as db:
        user = await _make_user(db)
        t1 = await _make_task(db, user, name="t1")
        t2 = await _make_task(db, user, name="t2")
        db.add_all([
            TaskRun(task_id=t1.id, status="success", seen=False),
            TaskRun(task_id=t1.id, status="error", seen=False),
            TaskRun(task_id=t1.id, status="running", seen=False),  # running 不计入
            TaskRun(task_id=t2.id, status="success", seen=True),   # 已读不计入
        ])
        await db.commit()

        result = await st_api.unread_count(current_user=user, db=db)
        assert result["total"] == 2
        assert result["by_task"] == {str(t1.id): 2}

        marked = await st_api.mark_runs_seen(t1.id, current_user=user, db=db)
        assert marked["marked"] == 2
        result = await st_api.unread_count(current_user=user, db=db)
        assert result["total"] == 0


@pytest.mark.asyncio
async def test_tick_batch_claims_multiple_due(db_maker, monkeypatch):
    """批量认领：一个 tick 认领全部到期任务并推进各自的 next_run_at"""
    dispatched = _capture_dispatch(monkeypatch)
    async with db_maker() as db:
        user = await _make_user(db)
        past = _naive(datetime.now(timezone.utc) - timedelta(minutes=1))
        tasks = []
        for i in range(3):
            t = ScheduledTask(
                user_id=user.id, name=f"task-{i}", prompt="x",
                cron_expr="* * * * *", recurring=True, timezone="UTC",
                next_run_at=past, enabled=True,
            )
            db.add(t)
            tasks.append(t)
        await db.commit()
        for t in tasks:
            await db.refresh(t)
        ids = {t.id for t in tasks}

    service = sched.SchedulerService()
    claimed = await service._tick()

    assert claimed == 3
    assert set(dispatched) == ids
    async with db_maker() as db:
        for tid in ids:
            t = await db.get(ScheduledTask, tid)
            assert t.next_run_at > past  # 全部已推进（相对旧值）


@pytest.mark.asyncio
async def test_loop_skips_sleep_when_backlogged(monkeypatch):
    """积压（认领取满上限）时循环不休眠，立即连续 tick；排空后恢复休眠"""
    service = sched.SchedulerService()
    ticks = []

    async def fake_tick():
        ticks.append(1)
        if len(ticks) >= 2:
            service._stopping = True
            return 0  # 排空
        return sched.MAX_CLAIM_PER_TICK  # 第一轮：取满，有积压

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(service, "_tick", fake_tick)  # 实例级 patch，避免类 patch 绑 self
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    await service._loop()

    assert len(ticks) == 2
    assert sleeps == [sched.TICK_INTERVAL]  # 积压轮不休眠，仅排空轮休眠一次


@pytest.mark.asyncio
async def test_create_task_with_delay(db_maker):
    """delay 相对时长由服务器时钟换算为一次性任务（调用方无需知道当前时间）"""
    async with db_maker() as db:
        user = await _make_user(db)
        before = datetime.now(timezone.utc)
        task = await st_api.create_task(
            st_api.ScheduledTaskCreate(name="提醒", prompt="喝水", delay="10m"),
            current_user=user, db=db,
        )
        after = datetime.now(timezone.utc)
        assert task.recurring is False
        assert task.cron_expr is None
        # next_run_at ≈ now + 10m（落在 :00/:30 时最多提前 90s jitter）
        next_aware = task.next_run_at.replace(tzinfo=timezone.utc)
        assert before + timedelta(minutes=10, seconds=-91) <= next_aware <= after + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_create_task_delay_validation(db_maker):
    """delay 非法或与 cron_expr 同时提供 → 400"""
    from fastapi import HTTPException

    async with db_maker() as db:
        user = await _make_user(db)
        with pytest.raises(HTTPException):
            await st_api.create_task(
                st_api.ScheduledTaskCreate(name="x", prompt="y", delay="abc"),
                current_user=user, db=db,
            )
        with pytest.raises(HTTPException):
            await st_api.create_task(
                st_api.ScheduledTaskCreate(name="x", prompt="y", delay="10m", cron_expr="0 9 * * *"),
                current_user=user, db=db,
            )


@pytest.mark.asyncio
async def test_update_task_with_delay(db_maker):
    """PATCH delay → 切换为一次性任务并按服务器时钟重算"""
    async with db_maker() as db:
        user = await _make_user(db)
        task = await _make_task(db, user)
        before = datetime.now(timezone.utc)
        result = await st_api.update_task(
            task.id, st_api.ScheduledTaskUpdate(delay="2h"), user, db,
        )
        after = datetime.now(timezone.utc)
        assert result.recurring is False
        assert result.cron_expr is None
        next_aware = result.next_run_at.replace(tzinfo=timezone.utc)
        assert before + timedelta(hours=2, seconds=-91) <= next_aware <= after + timedelta(hours=2)


@pytest.mark.asyncio
async def test_delete_task_cascades_runs_with_fk_enforced():
    """回归：删除任务级联删除执行记录（SQLite 开启 FK 强制，模拟 Postgres 行为）"""
    from sqlalchemy import event

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as db:
            user = await _make_user(db)
            task = await _make_task(db, user)
            db.add(TaskRun(task_id=task.id, status="success", seen=False))
            await db.commit()

            result = await st_api.delete_task(task.id, current_user=user, db=db)
            assert result == {"ok": True}
            assert await db.get(ScheduledTask, task.id) is None
            remaining = (await db.execute(
                select(TaskRun).where(TaskRun.task_id == task.id)
            )).scalars().all()
            assert remaining == []
    finally:
        await engine.dispose()
