"""session_search 服务测试 — FTS5 三路由分流、用户隔离、触发器同步"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.db.fts import ensure_fts
from app.models.message import Message
from app.models.thread import Thread
from app.models.user import User
from app.services.session_search import search_messages


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await ensure_fts(conn)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_user(db, username="tester") -> User:
    user = User(email=f"{username}@b.com", username=username, hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_thread(db, user, title="测试会话") -> Thread:
    thread = Thread(user_id=user.id, title=title)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def _add_message(db, thread, content, role="human", msg_type="text") -> Message:
    msg = Message(thread_id=thread.id, role=role, content=content, msg_type=msg_type)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


# ── 路由 1：非 CJK 走 unicode61 FTS5 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_english_keyword_hit(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user, title="部署讨论")
    await _add_message(db, thread, "how do I deploy with kubernetes?", role="human")
    await _add_message(db, thread, "use helm charts for kubernetes deploys", role="ai")

    sessions = await search_messages(db, user_id=user.id, query="kubernetes")
    assert len(sessions) == 1
    assert sessions[0]["thread_id"] == str(thread.id).replace("-", "")
    assert sessions[0]["title"] == "部署讨论"
    assert len(sessions[0]["matches"]) == 2
    assert "kubernetes" in sessions[0]["matches"][0]["snippet"]


@pytest.mark.asyncio
async def test_english_boolean_or(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "redis caching layer")

    sessions = await search_messages(db, user_id=user.id, query="postgres OR redis")
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_fts5_syntax_error_returns_empty(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "hello world")
    # 清洗后应为空 → 空结果而非异常
    sessions = await search_messages(db, user_id=user.id, query='"""')
    assert sessions == []


# ── 路由 2：CJK ≥3 字走 trigram FTS5 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cjk_long_token_trigram(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "我们讨论过大别山项目的方案", role="human")
    await _add_message(db, thread, "大别山项目分三期实施", role="ai")

    sessions = await search_messages(db, user_id=user.id, query="大别山项目")
    assert len(sessions) == 1
    assert len(sessions[0]["matches"]) == 2
    assert "大别山项目" in sessions[0]["matches"][0]["snippet"]


# ── 路由 3：CJK 短词 LIKE 兜底（hermes #20494：2 字词 trigram 匹配不到）─────


@pytest.mark.asyncio
async def test_cjk_short_token_like_fallback(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "周末去了桂林玩", role="human")

    # 2 字中文词必须能命中（走 LIKE 路由）
    sessions = await search_messages(db, user_id=user.id, query="桂林")
    assert len(sessions) == 1
    assert "桂林" in sessions[0]["matches"][0]["snippet"]


@pytest.mark.asyncio
async def test_cjk_short_or_query(db):
    user = await _make_user(db)
    t1 = await _make_thread(db, user, title="广西行")
    t2 = await _make_thread(db, user, title="桂林行")
    await _add_message(db, t1, "广西的米粉很好吃")
    await _add_message(db, t2, "桂林山水甲天下")

    sessions = await search_messages(db, user_id=user.id, query="广西 OR 桂林")
    assert len(sessions) == 2


# ── 作用域与排除 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_isolation(db):
    user = await _make_user(db, "tester")
    other = await _make_user(db, "other")
    t_mine = await _make_thread(db, user, title="我的")
    t_other = await _make_thread(db, other, title="别人的")
    await _add_message(db, t_mine, "my secret project alpha")
    await _add_message(db, t_other, "his secret project alpha")

    sessions = await search_messages(db, user_id=user.id, query="alpha")
    assert len(sessions) == 1
    assert sessions[0]["title"] == "我的"


@pytest.mark.asyncio
async def test_exclude_current_thread(db):
    user = await _make_user(db)
    current = await _make_thread(db, user, title="当前会话")
    old = await _make_thread(db, user, title="历史会话")
    await _add_message(db, current, "talking about zephyr now")
    await _add_message(db, old, "we discussed zephyr last week")

    sessions = await search_messages(
        db, user_id=user.id, query="zephyr", exclude_thread_id=str(current.id)
    )
    assert len(sessions) == 1
    assert sessions[0]["title"] == "历史会话"


@pytest.mark.asyncio
async def test_archived_thread_excluded(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "archived conversation about falcon")
    thread.is_archived = True
    db.add(thread)
    await db.commit()

    sessions = await search_messages(db, user_id=user.id, query="falcon")
    assert sessions == []


@pytest.mark.asyncio
async def test_max_sessions_clamped(db):
    user = await _make_user(db)
    for i in range(4):
        t = await _make_thread(db, user, title=f"会话{i}")
        await _add_message(db, t, f"common topic quasar in session {i}")

    sessions = await search_messages(db, user_id=user.id, query="quasar", max_sessions=2)
    assert len(sessions) == 2


# ── 上下文补全与触发器同步 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcript_includes_full_conversation(db):
    """transcript 应包含完整对话（未超预算时不裁剪），命中消息上下文都在"""
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "first message", role="human")
    await _add_message(db, thread, "mentions nebula here", role="ai")
    await _add_message(db, thread, "follow up question", role="human")

    sessions = await search_messages(db, user_id=user.id, query="nebula")
    transcript = sessions[0]["transcript"]
    assert "first message" in transcript
    assert "mentions nebula here" in transcript
    assert "follow up question" in transcript
    assert "truncated" not in transcript


@pytest.mark.asyncio
async def test_transcript_truncated_around_match(db):
    """长会话应围绕命中位置裁剪，保留命中词、带截断标记"""
    from app.services.session_search import _fetch_transcript

    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "开头" + "早" * 2000, role="human")
    await _add_message(db, thread, "中间提到了大别山项目的细节", role="ai")
    await _add_message(db, thread, "结尾" + "晚" * 2000, role="human")

    tid = str(thread.id).replace("-", "")
    transcript = await _fetch_transcript(db, tid, "大别山项目", max_chars=500)
    assert "大别山项目" in transcript
    assert "truncated" in transcript
    # 预算 500 + 前后截断标记，不应接近全文长度（全文 >4000 字符）
    assert len(transcript) < 1000


@pytest.mark.asyncio
async def test_transcript_formats_tool_calls(db):
    """tool_call 消息应渲染工具名与参数（content 为空时）"""
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    await _add_message(db, thread, "帮我查一下 kubernetes 部署", role="human")
    msg = Message(
        thread_id=thread.id, role="tool", content="", msg_type="tool_call",
        extra_metadata={"tool_name": "web_search", "tool_args": {"query": "kubernetes deploy"}},
    )
    db.add(msg)
    await db.commit()

    sessions = await search_messages(db, user_id=user.id, query="kubernetes")
    transcript = sessions[0]["transcript"]
    assert "TOOL:web_search" in transcript
    assert "kubernetes deploy" in transcript


@pytest.mark.asyncio
async def test_trigger_sync_on_update_and_delete(db):
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    msg = await _add_message(db, thread, "original text about phoenix")

    # UPDATE 后旧词不命中、新词命中
    msg.content = "rewritten text about griffin"
    db.add(msg)
    await db.commit()
    assert await search_messages(db, user_id=user.id, query="phoenix") == []
    assert len(await search_messages(db, user_id=user.id, query="griffin")) == 1

    # DELETE 后不再命中
    await db.delete(msg)
    await db.commit()
    assert await search_messages(db, user_id=user.id, query="griffin") == []


@pytest.mark.asyncio
async def test_empty_query(db):
    user = await _make_user(db)
    assert await search_messages(db, user_id=user.id, query="  ") == []


# ── 工具调用消息可搜索（content 为空时索引 tool_name/tool_args）─────────────


@pytest.mark.asyncio
async def test_tool_call_searchable_by_tool_name(db):
    """tool_call 消息 content 为空，工具名应可通过 FTS 命中（对齐 hermes）"""
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    msg = Message(
        thread_id=thread.id, role="tool", content="", msg_type="tool_call",
        extra_metadata={"type": "tool_call", "tool_name": "web_search",
                        "tool_args": {"query": "langgraph checkpoint"}},
    )
    db.add(msg)
    await db.commit()

    sessions = await search_messages(db, user_id=user.id, query="web_search")
    assert len(sessions) == 1
    assert sessions[0]["matches"][0]["msg_type"] == "tool_call"


@pytest.mark.asyncio
async def test_tool_call_searchable_by_args_cjk_like(db):
    """tool_args 里的中文短词走 LIKE 路由也应命中"""
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    msg = Message(
        thread_id=thread.id, role="tool", content="", msg_type="tool_call",
        extra_metadata={"type": "tool_call", "tool_name": "session_search",
                        "tool_args": {"query": "桂林"}},
    )
    db.add(msg)
    await db.commit()

    sessions = await search_messages(db, user_id=user.id, query="桂林")
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_tool_args_cjk_trigram_route(db):
    """tool_args 里的 ≥3 字中文经 json_tree 还原后应走 trigram 路由命中"""
    user = await _make_user(db)
    thread = await _make_thread(db, user)
    msg = Message(
        thread_id=thread.id, role="tool", content="", msg_type="tool_call",
        extra_metadata={"type": "tool_call", "tool_name": "web_search",
                        "tool_args": {"query": "大别山项目进展"}},
    )
    db.add(msg)
    await db.commit()

    sessions = await search_messages(db, user_id=user.id, query="大别山项目")
    assert len(sessions) == 1
    assert sessions[0]["matches"][0]["msg_type"] == "tool_call"
