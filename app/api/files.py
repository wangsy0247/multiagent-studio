"""
文件 API 路由: 上传、下载、列表、删除、outputs 产物下载/预览
"""

import logging
import mimetypes
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.file_record import FileRecord
from app.models.thread import Thread
from app.services.file_service import (
    save_upload,
    validate_file,
    get_upload_dir,
    get_outputs_dir,
    upload_virtual_path,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── outputs 产物下载/预览 (对齐 DeerFlow artifacts 安全规则) ────────────────

# 活性内容 (可在应用源执行脚本) 一律强制 attachment 下载, 防 XSS
ACTIVE_CONTENT_MIME_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
}
ACTIVE_CONTENT_EXTENSIONS = {".html", ".htm", ".xhtml", ".svg"}

# 文本类扩展名白名单 (MIME 识别失败时的兜底), 另支持无 NUL 字节嗅探
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".py", ".json", ".csv", ".tsv", ".log",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".js", ".jsx", ".ts", ".tsx", ".css", ".sql", ".sh",
}


def _build_content_disposition(disposition_type: str, filename: str) -> str:
    """RFC 5987 编码的 Content-Disposition (支持中文文件名)."""
    return f"{disposition_type}; filename*=UTF-8''{quote(filename)}"


def _is_text_file_by_content(path: Path, sample_size: int = 8192) -> bool:
    """嗅探前若干 KB, 无 NUL 字节则视为文本."""
    try:
        with open(path, "rb") as f:
            return b"\x00" not in f.read(sample_size)
    except OSError:
        return False


async def _get_owned_outputs_dir(
    thread_id: UUID, current_user: User, db: AsyncSession
) -> Path:
    """校验 thread 归属当前用户并返回其 outputs 物理目录."""
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return get_outputs_dir(current_user.username, str(thread_id))


def _resolve_outputs_file(outputs_dir: Path, file_path: str) -> Path:
    """resolve 后必须仍在 outputs 目录内 (路径穿越防护)."""
    base = outputs_dir.resolve()
    actual = (base / file_path).resolve()
    try:
        actual.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal detected")
    if not actual.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not actual.is_file():
        raise HTTPException(status_code=400, detail="路径不是文件")
    return actual


@router.get("/outputs/{thread_id}")
async def list_output_files(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出会话 outputs 目录下的产物文件清单."""
    outputs_dir = await _get_owned_outputs_dir(thread_id, current_user, db)
    files: list[dict] = []
    if outputs_dir.is_dir():
        for p in sorted(outputs_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(outputs_dir).as_posix()
            mime_type, _ = mimetypes.guess_type(p)
            files.append(
                {
                    "path": rel,
                    "virtual_path": f"/mnt/user-data/outputs/{rel}",
                    "filename": p.name,
                    "mime_type": mime_type or "application/octet-stream",
                    "size_bytes": p.stat().st_size,
                }
            )
    return {"files": files}


@router.get("/outputs/{thread_id}/{file_path:path}")
async def get_output_file(
    thread_id: UUID,
    file_path: str,
    download: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载/预览 outputs 产物文件.

    安全规则 (对齐 DeerFlow artifacts):
    - HTML/XHTML/SVG → 强制 attachment 下载 (防 XSS 在应用源执行)
    - 文本类 (text/* MIME / 扩展名白名单 / 无 NUL 嗅探) → 内联对应 MIME
    - 其他二进制 → FileResponse inline (starlette FileResponse 原生支持 Range 请求)
    - ``?download=true`` 强制下载; Content-Disposition 文件名按 RFC 5987 编码
    """
    outputs_dir = await _get_owned_outputs_dir(thread_id, current_user, db)
    actual = _resolve_outputs_file(outputs_dir, file_path)

    mime_type, _ = mimetypes.guess_type(actual)
    ext = actual.suffix.lower()
    is_active = mime_type in ACTIVE_CONTENT_MIME_TYPES or ext in ACTIVE_CONTENT_EXTENSIONS

    # 活性内容 / 显式下载 → 强制 attachment
    if download or is_active:
        return FileResponse(
            path=actual,
            media_type=mime_type or "application/octet-stream",
            headers={"Content-Disposition": _build_content_disposition("attachment", actual.name)},
        )

    # 文本类 → 内联
    if (
        (mime_type and mime_type.startswith("text/"))
        or ext in _TEXT_EXTENSIONS
        or _is_text_file_by_content(actual)
    ):
        try:
            content = actual.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = actual.read_bytes().decode("utf-8", errors="replace")
        return PlainTextResponse(content=content, media_type=mime_type or "text/plain")

    # 二进制 → inline 预览 (FileResponse 支持 Range, 供音视频拖动等场景)
    return FileResponse(
        path=actual,
        media_type=mime_type,
        headers={"Content-Disposition": _build_content_disposition("inline", actual.name)},
    )


@router.post("/upload")
async def upload_file(
    thread_id: UUID = Query(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传文件到会话的 Harness uploads 目录."""
    await validate_file(file)

    try:
        # DB 记录用 uuid 外键，文件系统路径统一用 username
        record = await save_upload(
            file, str(current_user.id), str(thread_id), fs_user_id=current_user.username
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return {
            "id": str(record.id),
            "filename": record.filename,
            "original_name": record.original_name,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "storage_path": record.storage_path,
            "virtual_path": upload_virtual_path(record.filename),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
async def list_files(
    thread_id: UUID = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出文件"""
    query = select(FileRecord).where(FileRecord.user_id == current_user.id)
    if thread_id:
        query = query.where(FileRecord.thread_id == thread_id)

    result = await db.execute(query.order_by(FileRecord.created_at.desc()))
    files = result.scalars().all()

    return {
        "files": [
            {
                "id": str(f.id),
                "filename": f.filename,
                "original_name": f.original_name,
                "mime_type": f.mime_type,
                "size_bytes": f.size_bytes,
                "virtual_path": upload_virtual_path(f.filename),
                "thread_id": str(f.thread_id) if f.thread_id else None,
                "created_at": f.created_at.isoformat(),
            }
            for f in files
        ]
    }


@router.get("/{file_id}")
async def download_file(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载文件"""
    result = await db.execute(
        select(FileRecord).where(FileRecord.id == file_id, FileRecord.user_id == current_user.id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 文件系统目录统一使用 username（record.user_id 是 uuid 外键，仅用于 DB 归属校验）
    full_path = get_upload_dir(current_user.username, str(record.thread_id)) / record.filename
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="文件已丢失")

    return FileResponse(
        str(full_path),
        filename=record.original_name,
        media_type=record.mime_type,
    )


@router.delete("/{file_id}")
async def delete_file(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除文件"""
    result = await db.execute(
        select(FileRecord).where(FileRecord.id == file_id, FileRecord.user_id == current_user.id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 删除物理文件（文件系统目录统一使用 username）
    full_path = get_upload_dir(current_user.username, str(record.thread_id)) / record.filename
    if full_path.is_file():
        try:
            full_path.unlink()
        except OSError as exc:
            logger.warning("Failed to delete upload file %s: %s", full_path, exc)

    # 删除数据库记录
    await db.delete(record)
    await db.commit()

    return {"success": True, "message": "文件已删除"}
