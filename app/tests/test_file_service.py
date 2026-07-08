"""Tests for app file upload service."""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest
from fastapi import UploadFile

# Use a temporary Harness data root for all tests.
@pytest.fixture(autouse=True)
def temp_data_root(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="app_uploads_test_")
    monkeypatch.setenv("HARNESS_DATA_ROOT", tmp)
    yield tmp


def test_upload_dir_uses_harness_layout():
    from app.services.file_service import get_upload_dir

    d = get_upload_dir("user-u1", "thread-t1")
    expected_suffix = Path("users/user-u1/threads/thread-t1/user-data/uploads")
    assert d.relative_to(d.parent.parent.parent.parent.parent.parent) == expected_suffix


def test_is_allowed_file_accepts_allowed_mime():
    from app.services.file_service import _is_allowed_file

    assert _is_allowed_file("report.pdf", "application/pdf") is True
    assert _is_allowed_file("image.png", "image/png") is True
    assert _is_allowed_file("data.json", "application/json") is True


def test_is_allowed_file_accepts_octet_stream_with_allowed_extension():
    from app.services.file_service import _is_allowed_file

    assert _is_allowed_file("script.py", "application/octet-stream") is True
    assert _is_allowed_file("README.md", "application/octet-stream") is True
    assert _is_allowed_file("app.js", "application/octet-stream") is True


def test_is_allowed_file_rejects_octet_stream_with_unknown_extension():
    from app.services.file_service import _is_allowed_file

    assert _is_allowed_file("binary.exe", "application/octet-stream") is False
    assert _is_allowed_file("file.bin", "application/octet-stream") is False


def test_is_allowed_file_accepts_allowed_extension_without_content_type():
    from app.services.file_service import _is_allowed_file

    assert _is_allowed_file("main.py", None) is True
    assert _is_allowed_file("style.css", None) is True
    assert _is_allowed_file("data.yaml", None) is True


def test_is_allowed_file_rejects_unknown_files():
    from app.services.file_service import _is_allowed_file

    assert _is_allowed_file("unknown", None) is False
    assert _is_allowed_file(None, "application/octet-stream") is False


@pytest.mark.asyncio
async def test_validate_file_accepts_poscar():
    """POSCAR has no extension and octet-stream MIME, but is valid UTF-8 text."""
    from app.services.file_service import validate_file

    poscar = """POSCAR_test
    1.0
    2.0 0.0 0.0
    0.0 2.0 0.0
    0.0 0.0 2.0
    H
    1
    Direct
    0.0 0.0 0.0
    """
    file = UploadFile(filename="POSCAR", file=io.BytesIO(poscar.encode("utf-8")))
    file.size = len(poscar.encode("utf-8"))
    await validate_file(file)


@pytest.mark.asyncio
async def test_validate_file_rejects_binary_without_extension():
    from app.services.file_service import validate_file

    file = UploadFile(filename="binary", file=io.BytesIO(b"\x00\x01\x02\x03"))
    file.size = 4
    with pytest.raises(ValueError, match="不支持的文件类型"):
        await validate_file(file)


@pytest.mark.asyncio
async def test_save_upload_preserves_poscar_name():
    """Saving a POSCAR file should keep its original name."""
    from app.services.file_service import save_upload, get_upload_dir

    poscar = "POSCAR_test\n1.0\n"
    file = UploadFile(filename="POSCAR", file=io.BytesIO(poscar.encode("utf-8")))
    file.size = len(poscar.encode("utf-8"))
    record = await save_upload(file, "user-u1", "thread-t1")

    assert record.filename == "POSCAR"
    saved_path = get_upload_dir("user-u1", "thread-t1") / "POSCAR"
    assert saved_path.read_text(encoding="utf-8") == poscar
