"""Tests for unified username-based filesystem user identity.

覆盖:
- RegisterRequest username 路径安全校验 + 保留名
- resolve_fs_user_id: explicit 优先 / JWT sub(uuid) → username 翻译 / 兜底
- save_upload 的 fs_user_id (DB 记录用 uuid, 文件落盘用 username)
"""
from __future__ import annotations

import io
import uuid as uuid_mod

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel


# ── RegisterRequest username 校验 ─────────────────────────────────────────

def test_username_valid_forms():
    from app.schemas.auth import RegisterRequest

    for name in ("alice", "123", "Ai-Engineer_01", "a_b-c-1"):
        req = RegisterRequest(email="a@b.com", username=name, password="secret1")
        assert req.username == name


def test_username_rejects_path_unsafe_chars():
    from app.schemas.auth import RegisterRequest

    for name in ("alice bob", "alice@x", "a/b", "a\\b", "a.b", "中文名"):
        with pytest.raises(ValidationError):
            RegisterRequest(email="a@b.com", username=name, password="secret1")


def test_username_rejects_reserved_names():
    from app.schemas.auth import RegisterRequest

    for name in ("default", "anonymous", "Default", "ANONYMOUS"):
        with pytest.raises(ValidationError, match="保留名"):
            RegisterRequest(email="a@b.com", username=name, password="secret1")


# ── resolve_fs_user_id ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session():
    """内存 SQLite 会话，仅建 users 表。"""
    import app.models.user  # noqa: F401 — 注册 User 到 metadata

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_user(session: AsyncSession, username: str) -> uuid_mod.UUID:
    from app.models.user import User

    user = User(
        email=f"{username}@test.com",
        username=username,
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


@pytest.mark.asyncio
async def test_resolve_explicit_wins(db_session):
    from app.api.deps import resolve_fs_user_id

    assert await resolve_fs_user_id("alice", None, db_session) == "alice"


@pytest.mark.asyncio
async def test_resolve_jwt_sub_translated_to_username(db_session):
    from app.api.deps import resolve_fs_user_id
    from app.services.auth_service import create_access_token

    uid = await _make_user(db_session, "alice")
    token = create_access_token(str(uid), "user")

    # explicit 缺省 / "default" 时，JWT sub (uuid) 必须翻译成 username
    assert await resolve_fs_user_id(None, f"Bearer {token}", db_session) == "alice"
    assert await resolve_fs_user_id("default", f"Bearer {token}", db_session) == "alice"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default(db_session):
    from app.api.deps import resolve_fs_user_id
    from app.services.auth_service import create_access_token

    # 无 token
    assert await resolve_fs_user_id(None, None, db_session) == "default"
    # token 中的用户已被删除
    ghost = create_access_token(str(uuid_mod.uuid4()), "user")
    assert await resolve_fs_user_id(None, f"Bearer {ghost}", db_session) == "default"
    # 非法 token
    assert await resolve_fs_user_id(None, "Bearer not-a-token", db_session) == "default"


# ── save_upload fs_user_id ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_upload_separates_db_and_fs_identities(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_ROOT", str(tmp_path))
    from fastapi import UploadFile
    from app.services.file_service import save_upload, get_upload_dir

    file = UploadFile(filename="note.md", file=io.BytesIO(b"hello"))
    file.size = 5
    record = await save_upload(file, "uuid-1234", "thread-t1", fs_user_id="alice")

    # DB 记录保持 uuid 外键
    assert record.user_id == "uuid-1234"
    # 文件落在 username 目录下
    saved = get_upload_dir("alice", "thread-t1") / "note.md"
    assert saved.read_text() == "hello"
    assert "users/alice/" in record.storage_path
