"""
文件服务: 上传到 {workspace}/{user_id}/{thread_id}/uploads/
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import UploadFile

from app.models.file_record import FileRecord

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.path.expanduser(os.getenv("WORKSPACE_ROOT", "~/.multiagent-studio/workspace"))
UPLOAD_MAX_SIZE_MB = int(os.getenv("UPLOAD_MAX_SIZE_MB", "50"))
UPLOAD_MAX_SIZE_BYTES = UPLOAD_MAX_SIZE_MB * 1024 * 1024

ALLOWED_MIME_TYPES = {
    "text/plain", "text/csv", "text/markdown", "text/html",
    "application/json", "application/xml", "application/yaml",
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/svg+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/zip", "application/gzip",
}


def get_upload_dir(user_id: str, thread_id: str) -> str:
    """生成上传目录路径"""
    return os.path.join(WORKSPACE_ROOT, user_id, thread_id, "uploads")


def validate_file(file: UploadFile) -> None:
    """验证文件类型和大小"""
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"不支持的文件类型: {file.content_type}")

    if file.size and file.size > UPLOAD_MAX_SIZE_BYTES:
        raise ValueError(f"文件过大 (最大 {UPLOAD_MAX_SIZE_MB}MB)")


async def save_upload(file: UploadFile, user_id: str, thread_id: str) -> FileRecord:
    """保存上传文件并返回元数据记录"""
    upload_dir = get_upload_dir(user_id, thread_id)
    os.makedirs(upload_dir, exist_ok=True)

    # 生成唯一文件名
    ext = os.path.splitext(file.filename or "file")[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    storage_path = os.path.join(user_id, thread_id, "uploads", stored_name)
    full_path = os.path.join(WORKSPACE_ROOT, storage_path)

    # 写入文件
    content = await file.read()
    with open(full_path, "wb") as f:
        f.write(content)

    return FileRecord(
        user_id=user_id,
        thread_id=thread_id,
        filename=stored_name,
        original_name=file.filename or "unknown",
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(content),
        storage_path=storage_path,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
