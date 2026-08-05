"""outputs 产物下载/预览端点测试（产物出口闭环，对齐 DeerFlow artifacts 安全规则）"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api import files as files_api
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


async def _make_user_and_thread(db, username="tester") -> tuple[User, Thread]:
    user = User(email=f"{username}@b.com", username=username, hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    thread = Thread(user_id=user.id, title="t")
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return user, thread


def _write_output(data_root: Path, username: str, thread_id, rel: str, content: bytes) -> Path:
    from app.services.file_service import get_outputs_dir

    outputs = get_outputs_dir(username, str(thread_id))
    p = outputs / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


# ── 正常下载 / 预览 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_text_output_inline(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "report.md", "# 报告\n你好".encode())

    resp = await files_api.get_output_file(thread.id, "report.md", False, user, db)
    assert isinstance(resp, PlainTextResponse)
    assert "报告" in resp.body.decode("utf-8")
    assert "attachment" not in resp.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_get_binary_output_inline(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "chart.png", b"\x89PNG\x00\x01")

    resp = await files_api.get_output_file(thread.id, "chart.png", False, user, db)
    assert isinstance(resp, FileResponse)
    cd = resp.headers["content-disposition"]
    assert cd.startswith("inline; filename*=UTF-8''")


@pytest.mark.asyncio
async def test_download_true_forces_attachment(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "report.md", b"hello")

    resp = await files_api.get_output_file(thread.id, "report.md", True, user, db)
    assert isinstance(resp, FileResponse)
    assert resp.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")


@pytest.mark.asyncio
async def test_html_forced_attachment(db, temp_data_root):
    """HTML/SVG 活性内容强制 attachment, 防 XSS 在应用源执行."""
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "page.html", b"<script>alert(1)</script>")
    _write_output(temp_data_root, user.username, thread.id, "icon.svg", b"<svg></svg>")

    for name in ("page.html", "icon.svg"):
        resp = await files_api.get_output_file(thread.id, name, False, user, db)
        assert isinstance(resp, FileResponse)
        assert resp.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_chinese_filename_rfc5987(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "分析报告.csv", b"a,b\n1,2")

    resp = await files_api.get_output_file(thread.id, "分析报告.csv", True, user, db)
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment; filename*=UTF-8''")
    assert "%E5%88%86%E6%9E%90" in cd  # RFC 5987 percent-encoded


@pytest.mark.asyncio
async def test_subdirectory_file(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "sub/notes.txt", b"nested")

    resp = await files_api.get_output_file(thread.id, "sub/notes.txt", False, user, db)
    assert isinstance(resp, PlainTextResponse)
    assert resp.body.decode() == "nested"


# ── 安全: 路径穿越 / 越权 / 不存在 ───────────────────────────


@pytest.mark.asyncio
async def test_path_traversal_rejected(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    # uploads 里放一个文件, 尝试用 ../ 穿越过去
    uploads = temp_data_root / "users" / user.username / "threads" / str(thread.id) / "user-data" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "secret.txt").write_text("secret")

    with pytest.raises(HTTPException) as exc:
        await files_api.get_output_file(thread.id, "../uploads/secret.txt", False, user, db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_absolute_path_escape_rejected(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    with pytest.raises(HTTPException) as exc:
        await files_api.get_output_file(thread.id, "/etc/passwd", False, user, db)
    assert exc.value.status_code in (403, 404)


@pytest.mark.asyncio
async def test_other_users_thread_not_accessible(db, temp_data_root):
    owner, thread = await _make_user_and_thread(db, username="owner")
    _write_output(temp_data_root, owner.username, thread.id, "report.md", b"x")
    other = User(email="other@b.com", username="other", hashed_password="x")
    db.add(other)
    await db.commit()
    await db.refresh(other)

    with pytest.raises(HTTPException) as exc:
        await files_api.get_output_file(thread.id, "report.md", False, other, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_missing_file_404(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    with pytest.raises(HTTPException) as exc:
        await files_api.get_output_file(thread.id, "nope.txt", False, user, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_random_thread_id_404(db):
    user, _ = await _make_user_and_thread(db)
    with pytest.raises(HTTPException) as exc:
        await files_api.get_output_file(uuid.uuid4(), "x.txt", False, user, db)
    assert exc.value.status_code == 404


# ── 列表端点 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_output_files(db, temp_data_root):
    user, thread = await _make_user_and_thread(db)
    _write_output(temp_data_root, user.username, thread.id, "a.md", b"a")
    _write_output(temp_data_root, user.username, thread.id, "sub/b.txt", b"bb")

    result = await files_api.list_output_files(thread.id, user, db)
    by_path = {f["path"]: f for f in result["files"]}
    assert set(by_path) == {"a.md", "sub/b.txt"}
    assert by_path["a.md"]["size_bytes"] == 1
    assert by_path["sub/b.txt"]["virtual_path"] == "/mnt/user-data/outputs/sub/b.txt"


@pytest.mark.asyncio
async def test_list_output_files_empty(db):
    user, thread = await _make_user_and_thread(db)
    result = await files_api.list_output_files(thread.id, user, db)
    assert result == {"files": []}
