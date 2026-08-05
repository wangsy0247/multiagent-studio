"""SSE 断线续传 (Phase 3) 测试 — StreamHub 序号/缓冲/广播 + resume 端点"""

import asyncio
import json
import uuid as uuid_mod

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api import execute as execute_module
from app.api.deps import get_current_user
from app.db.engine import get_db
from app.models.message import Message
from app.models.thread import Thread
from app.models.user import User
from app.services.stream_hub import (
    QUEUE_MAX,
    StreamHub,
    SUB_GAP,
    SUB_NOT_RUNNING,
    SUB_OK,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _make_user_and_thread(maker) -> tuple[User, Thread]:
    async with maker() as db:
        user = User(email="a@b.com", username="tester", hashed_password="x")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        thread = Thread(user_id=user.id, title="t", status="running")
        db.add(thread)
        await db.commit()
        await db.refresh(thread)
        return user, thread


def _make_hub() -> StreamHub:
    """测试用独立 hub 实例, 并替换 execute 模块取到的全局单例"""
    import app.services.stream_hub as hub_module

    hub = StreamHub()
    hub_module._hub = hub
    return hub


async def _drain_queue(q: asyncio.Queue) -> list:
    """读订阅队列直到结束哨兵, 返回 (seq, payload) 列表"""
    out = []
    while True:
        item = await q.get()
        if item is None:
            return out
        out.append(item)


# ── StreamHub: 序号 / 缓冲 / 订阅 ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_assigns_monotonic_seq():
    hub = _make_hub()
    rs = hub.start_run("t1")
    assert hub.publish(rs, '{"a":1}') == 1
    assert hub.publish(rs, '{"a":2}') == 2
    assert [s for s, _ in rs.buffer] == [1, 2]


@pytest.mark.asyncio
async def test_subscribe_replays_missed_events():
    hub = _make_hub()
    rs = hub.start_run("t1")
    for i in range(3):
        hub.publish(rs, f'{{"n":{i}}}')

    sub = hub.subscribe("t1", last_event_id=1)
    assert sub.status == SUB_OK
    assert [s for s, _ in sub.missed] == [2, 3]

    # 全量补发 (last_event_id=0, 页面刷新后重建用)
    sub_full = hub.subscribe("t1", last_event_id=0)
    assert [s for s, _ in sub_full.missed] == [1, 2, 3]


@pytest.mark.asyncio
async def test_subscribe_live_events_after_replay():
    hub = _make_hub()
    rs = hub.start_run("t1")
    hub.publish(rs, '{"n":0}')
    sub = hub.subscribe("t1", last_event_id=0)
    hub.publish(rs, '{"n":1}')
    assert await sub.queue.get() == (2, '{"n":1}')


@pytest.mark.asyncio
async def test_subscribe_not_running_and_gap():
    hub = _make_hub()
    assert hub.subscribe("nope").status == SUB_NOT_RUNNING

    rs = hub.start_run("t1")
    # 人工构造缓冲截断: 最旧 seq=5, last_event_id=2 → 3/4 丢失
    for i in range(5, 8):
        rs.seq = i - 1
        hub.publish(rs, f'{{"n":{i}}}')
    assert hub.subscribe("t1", last_event_id=2).status == SUB_GAP
    assert hub.subscribe("t1", last_event_id=4).status == SUB_OK

    hub.end_run(rs)
    assert hub.subscribe("t1", last_event_id=0).status == SUB_NOT_RUNNING


@pytest.mark.asyncio
async def test_end_run_sends_sentinel_and_is_idempotent():
    hub = _make_hub()
    rs = hub.start_run("t1")
    hub.publish(rs, '{"n":0}')
    sub = hub.subscribe("t1", last_event_id=0)
    hub.end_run(rs)
    hub.end_run(rs)  # 幂等
    items = await _drain_queue(sub.queue)
    assert items == []  # 补发事件在 missed 里, 队列只有哨兵


@pytest.mark.asyncio
async def test_backpressure_drops_slow_consumer():
    hub = _make_hub()
    rs = hub.start_run("t1")
    sub = hub.subscribe("t1", last_event_id=0)
    # 不消费, 直接灌满队列
    for i in range(QUEUE_MAX + 5):
        hub.publish(rs, f'{{"n":{i}}}')
    # 该订阅者已被丢弃, 队列尾部是结束哨兵 → 流正常收尾
    items = await _drain_queue(sub.queue)
    assert len(items) <= QUEUE_MAX
    assert sub.queue not in rs.subscribers


@pytest.mark.asyncio
async def test_start_run_replaces_old_state():
    hub = _make_hub()
    rs1 = hub.start_run("t1")
    sub1 = hub.subscribe("t1", last_event_id=0)
    rs2 = hub.start_run("t1")
    assert rs2.seq == 0 and not rs1.running
    # 旧订阅者收到结束哨兵
    assert await sub1.queue.get() is None


# ── _pump_run: 序号广播 + 落库 + 断点续传 ──────────────────────


async def _collect(sub) -> list[tuple[int, dict]]:
    """消费订阅 (missed + 实时队列) 直到哨兵, 返回 (seq, event) 列表"""
    out = [(s, json.loads(p)) for s, p in sub.missed]
    while True:
        item = await sub.queue.get()
        if item is None:
            return out
        out.append((item[0], json.loads(item[1])))


@pytest.mark.asyncio
async def test_pump_broadcasts_with_seq_and_persists(session_factory):
    user, thread = await _make_user_and_thread(session_factory)
    hub = _make_hub()
    tid = str(thread.id)

    events = [
        {"type": "message", "content": "你好", "thread_id": tid},
        {"type": "message", "content": "世界", "thread_id": tid},
        {"type": "finished", "thread_id": tid},
    ]

    async def stream_factory():
        for e in events:
            yield json.dumps(e)
            await asyncio.sleep(0)

    rs = hub.start_run(tid)
    rs.pump_task = asyncio.create_task(
        execute_module._pump_run(tid, thread.id, rs, stream_factory,
                                 session_factory=session_factory)
    )
    sub = hub.subscribe(tid, last_event_id=0)
    got = await _collect(sub)
    await rs.pump_task

    assert [s for s, _ in got] == [1, 2, 3]
    assert got[-1][1]["type"] == "finished"
    assert not rs.running

    # 落库: 两个流式 chunk 合并为一条 ai 消息 + 线程终态
    async with session_factory() as db:
        from sqlalchemy import select
        msgs = (await db.execute(
            select(Message).where(Message.thread_id == thread.id)
        )).scalars().all()
        assert any(m.role == "ai" and m.content == "你好世界" for m in msgs)
        t = (await db.execute(
            select(Thread).where(Thread.id == thread.id)
        )).scalar_one()
        assert t.status == "finished"


@pytest.mark.asyncio
async def test_resume_mid_run_replays_then_live(session_factory):
    """模拟刷新: 第一个消费者读 2 条后断开, resume 消费者从断点补发+续流"""
    user, thread = await _make_user_and_thread(session_factory)
    hub = _make_hub()
    tid = str(thread.id)

    gate = asyncio.Event()

    async def stream_factory():
        for i in range(2):
            yield json.dumps({"type": "message", "content": f"c{i}", "thread_id": tid})
        await gate.wait()  # 前 2 条后暂停, 等"刷新"发生
        yield json.dumps({"type": "message", "content": "c2", "thread_id": tid})
        yield json.dumps({"type": "finished", "thread_id": tid})

    rs = hub.start_run(tid)
    rs.pump_task = asyncio.create_task(
        execute_module._pump_run(tid, thread.id, rs, stream_factory,
                                 session_factory=session_factory)
    )
    sub1 = hub.subscribe(tid, last_event_id=0)
    first = [await sub1.queue.get(), await sub1.queue.get()]
    assert [s for s, _ in first] == [1, 2]
    # 原客户端断开 (页面刷新) — 只退订, 泵继续跑
    hub.unsubscribe(rs, sub1.queue)

    # resume: last_event_id=2 → 无补发, 等实时事件
    sub2 = hub.subscribe(tid, last_event_id=2)
    assert sub2.status == SUB_OK and sub2.missed == []
    gate.set()
    got = await _collect(sub2)
    await rs.pump_task
    assert [s for s, _ in got] == [3, 4]
    assert got[-1][1]["type"] == "finished"


@pytest.mark.asyncio
async def test_pump_survives_harness_failure(session_factory):
    """Harness 异常 → 泵广播 error 终态事件并落库 error 状态"""
    user, thread = await _make_user_and_thread(session_factory)
    hub = _make_hub()
    tid = str(thread.id)

    async def stream_factory():
        yield json.dumps({"type": "message", "content": "x", "thread_id": tid})
        raise RuntimeError("boom")

    rs = hub.start_run(tid)
    rs.pump_task = asyncio.create_task(
        execute_module._pump_run(tid, thread.id, rs, stream_factory,
                                 session_factory=session_factory)
    )
    sub = hub.subscribe(tid, last_event_id=0)
    got = await _collect(sub)
    await rs.pump_task
    assert got[-1][1]["type"] == "error"
    assert "boom" in got[-1][1]["content"]


# ── resume 端点 (HTTP 层) ─────────────────────────────────────


@pytest_asyncio.fixture
async def api_client(session_factory):
    user, thread = await _make_user_and_thread(session_factory)
    _make_hub()

    app = FastAPI()
    app.include_router(execute_module.router, prefix="/api/execute")
    app.dependency_overrides[get_current_user] = lambda: user

    async def _get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, user, thread


def _parse_sse(body: str) -> list[tuple[int | None, dict]]:
    frames = []
    seq = None
    for line in body.splitlines():
        if line.startswith("id: "):
            seq = int(line[4:])
        elif line.startswith("data: "):
            frames.append((seq, json.loads(line[6:])))
            seq = None
    return frames


@pytest.mark.asyncio
async def test_resume_endpoint_not_running(api_client):
    client, user, thread = api_client
    resp = await client.post(f"/api/execute/{thread.id}/resume?last_event_id=0")
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert frames == [(None, {"type": "resync", "reason": "not_running"})]


@pytest.mark.asyncio
async def test_resume_endpoint_gap_returns_resync(api_client):
    client, user, thread = api_client
    hub = execute_module.get_stream_hub()
    tid = str(thread.id)
    rs = hub.start_run(tid)
    for i in range(5, 8):
        rs.seq = i - 1
        hub.publish(rs, json.dumps({"type": "message", "content": f"c{i}"}))

    resp = await client.post(f"/api/execute/{thread.id}/resume?last_event_id=2")
    frames = _parse_sse(resp.text)
    assert frames == [(None, {"type": "resync", "reason": "gap"})]


@pytest.mark.asyncio
async def test_resume_endpoint_404_for_other_user(api_client, session_factory):
    client, user, thread = api_client
    other = Thread(user_id=uuid_mod.uuid4(), title="x")
    async with session_factory() as db:
        db.add(other)
        await db.commit()
        await db.refresh(other)
        oid = other.id
    resp = await client.post(f"/api/execute/{oid}/resume?last_event_id=0")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resume_endpoint_full_flow(api_client, session_factory):
    """端到端: 泵在跑 → resume 补发 missed → 续流 → finished 帧带 id"""
    client, user, thread = api_client
    hub = execute_module.get_stream_hub()
    tid = str(thread.id)

    gate = asyncio.Event()

    async def stream_factory():
        yield json.dumps({"type": "message", "content": "前", "thread_id": tid})
        await gate.wait()
        yield json.dumps({"type": "message", "content": "后", "thread_id": tid})
        yield json.dumps({"type": "finished", "thread_id": tid})

    rs = hub.start_run(tid)
    rs.pump_task = asyncio.create_task(
        execute_module._pump_run(tid, thread.id, rs, stream_factory,
                                 session_factory=session_factory)
    )
    # 等泵发出第 1 条并在 gate 前停住
    for _ in range(100):
        if rs.seq >= 1:
            break
        await asyncio.sleep(0.01)
    assert rs.seq == 1

    req_task = asyncio.create_task(
        client.post(f"/api/execute/{tid}/resume?last_event_id=0")
    )
    # 等 resume 订阅挂到泵上再放行后续事件, 消除竞态
    for _ in range(100):
        if rs.subscribers:
            break
        await asyncio.sleep(0.01)
    gate.set()
    resp = await req_task
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert [s for s, _ in frames] == [1, 2, 3]
    assert frames[-1][1]["type"] == "finished"
    await rs.pump_task


@pytest.mark.asyncio
async def test_pump_does_not_hold_db_connection(tmp_path):
    """回归: 泵任务运行期间不得长占连接池 (SQLite pool_size=1)。

    旧实现中 _pump_run 起始的 SELECT 未提交, 事务在整个运行期间持有
    池内唯一连接, 其他请求在 get_current_user 处等连接 30s 超时。
    修复: 起始 SELECT 后立即 commit, 后续 commit 点均为短暂事务。
    注意: 必须用文件库复现 — 内存库默认 StaticPool 不受 pool_size 约束。
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/t.db",
        pool_size=1, max_overflow=0, pool_timeout=1,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        user, thread = await _make_user_and_thread(maker)
        hub = _make_hub()
        tid = str(thread.id)

        gate = asyncio.Event()

        async def stream_factory():
            yield json.dumps({"type": "message", "content": "c0", "thread_id": tid})
            await gate.wait()  # 泵在此挂起, 模拟长时间运行
            yield json.dumps({"type": "finished", "thread_id": tid})

        rs = hub.start_run(tid)
        rs.pump_task = asyncio.create_task(
            execute_module._pump_run(tid, thread.id, rs, stream_factory,
                                     session_factory=maker)
        )
        # 等泵发出第 1 条并停在 gate 前
        for _ in range(100):
            if rs.seq >= 1:
                break
            await asyncio.sleep(0.01)
        assert rs.seq == 1

        # 泵挂起期间, 其他 session 必须能在 pool_timeout 内拿到连接
        from sqlalchemy import select
        async with maker() as db:
            t = (await db.execute(
                select(Thread).where(Thread.id == thread.id)
            )).scalar_one()
            assert t.status == "running"

        gate.set()
        await rs.pump_task
    finally:
        await engine.dispose()
