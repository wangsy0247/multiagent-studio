"""
SQLModel 会话模型
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import JSON, Field, SQLModel, Column


class Thread(SQLModel, table=True):
    __tablename__ = "threads"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    title: str = Field(default="新会话", max_length=200)
    status: str = Field(default="idle", max_length=20)  # idle|running|suspended|finished|error
    execution_graph: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    preset_type: Optional[str] = Field(default=None, max_length=50)
    extra_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    is_archived: bool = Field(default=False)
    # ── Agent Team 字段 ──
    project_id: Optional[str] = Field(default=None, max_length=50, index=True)
    agent_name: Optional[str] = Field(default=None, max_length=100)
    mode: str = Field(default="single", max_length=20)  # "single" | "team"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
