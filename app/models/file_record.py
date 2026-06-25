"""
SQLModel 文件记录模型
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class FileRecord(SQLModel, table=True):
    __tablename__ = "file_records"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    thread_id: Optional[uuid.UUID] = Field(default=None, foreign_key="threads.id", index=True)
    filename: str = Field(max_length=500)
    original_name: str = Field(max_length=500)
    mime_type: str = Field(max_length=100)
    size_bytes: int = Field(default=0)
    storage_path: str = Field(max_length=1000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
