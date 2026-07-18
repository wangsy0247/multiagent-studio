"""
文件 API 路由: 上传、下载、列表、删除
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.file_record import FileRecord
from app.services.file_service import save_upload, validate_file, get_upload_dir, upload_virtual_path

logger = logging.getLogger(__name__)
router = APIRouter()


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
