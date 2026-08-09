"""_StreamPersister.flush() 回归测试 — 非 finished 终态(澄清暂停/错误/取消)
下已产生的 AI 文本必须落库, 否则刷新后"最终回答消失"。"""
from __future__ import annotations

import uuid as uuid_mod

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api.execute import _StreamPersister
from app.models.message import Message

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


async def _messages(db, thread_uuid):
    result = await db.execute(
        select(Message).where(Message.thread_id == thread_uuid).order_by(Message.created_at, Message.id)
    )
    return result.scalars().all()


async def test_flush_persists_accumulated_text_on_clarification(db):
    """澄清暂停: 暂停前的 AI 文本经 flush() 落库, token 不回填."""
    tid = uuid_mod.uuid4()
    p = _StreamPersister(db, tid)
    p.handle("message", {"type": "message", "content": "我先分析一下,"})
    p.handle("message", {"type": "message", "content": "需要向你确认一个问题。"})
    p.handle("token_usage", {"type": "token_usage", "tokens": {"total_tokens": 100, "prompt_tokens": 80, "completion_tokens": 20}})
    p.flush()  # 模拟 clarification 到达
    await db.commit()

    msgs = await _messages(db, tid)
    assert len(msgs) == 1
    assert msgs[0].role == "ai"
    assert "需要向你确认一个问题" in msgs[0].content
    assert msgs[0].token_count == 0  # flush 不回填 token (token 留给最终回答)


async def test_flush_preserves_tool_segments(db):
    """tool_call 边界切段 + flush: 中间段与末尾段都完整落库."""
    tid = uuid_mod.uuid4()
    p = _StreamPersister(db, tid)
    p.handle("message", {"type": "message", "content": "第一步结果"})
    p.handle("tool_call", {"type": "tool_call", "tool_name": "web_search", "tool_args": {}})
    p.handle("message", {"type": "message", "content": "出错前的部分回答"})
    p.flush()  # 模拟 error 到达
    await db.commit()

    msgs = await _messages(db, tid)
    texts = [m.content for m in msgs if m.role == "ai"]
    assert any("第一步结果" in t for t in texts)
    assert any("出错前的部分回答" in t for t in texts)


async def test_finalize_still_backfills_tokens(db):
    """finished 路径行为不变: 最后一段回填 token + extra_metadata."""
    tid = uuid_mod.uuid4()
    p = _StreamPersister(db, tid)
    p.handle("message", {"type": "message", "content": "最终回答"})
    p.handle("token_usage", {"type": "token_usage", "tokens": {
        "prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
        "cache_hit_tokens": 10, "cache_miss_tokens": 70,
    }})
    p.finalize()
    await db.commit()

    msgs = await _messages(db, tid)
    assert len(msgs) == 1
    assert msgs[0].token_count == 100
    tokens = (msgs[0].extra_metadata or {}).get("tokens")
    assert tokens and tokens["cache_hit_tokens"] == 10


async def test_flush_empty_is_noop(db):
    """无累积内容时 flush 不产生空消息."""
    tid = uuid_mod.uuid4()
    p = _StreamPersister(db, tid)
    p.flush()
    await db.commit()
    assert await _messages(db, tid) == []
