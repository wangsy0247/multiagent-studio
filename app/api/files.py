"""
文件 API 路由: 上传、下载、列表、删除
"""

import os
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
from app.services.file_service import save_upload, validate_file, WORKSPACE_ROOT

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/upload")
async def upload_file(
    thread_id: UUID = Query(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传文件到会话"""
    validate_file(file)

    try:
        record = await save_upload(file, str(current_user.id), str(thread_id))
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return {
            "id": str(record.id),
            "filename": record.original_name,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "storage_path": record.storage_path,
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
                "filename": f.original_name,
                "mime_type": f.mime_type,
                "size_bytes": f.size_bytes,
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

    full_path = os.path.join(WORKSPACE_ROOT, record.storage_path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="文件已丢失")

    return FileResponse(
        full_path,
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

    # 删除物理文件
    full_path = os.path.join(WORKSPACE_ROOT, record.storage_path)
    if os.path.exists(full_path):
        os.remove(full_path)

    # 删除数据库记录
    await db.delete(record)
    await db.commit()

    return {"success": True, "message": "文件已删除"}
