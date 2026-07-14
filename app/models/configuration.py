"""
SQLModel 用户配置模型
"""

import uuid
from datetime import datetime, timezone

from sqlmodel import JSON, Field, SQLModel, Column


class UserConfig(SQLModel, table=True):
    __tablename__ = "user_configurations"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, unique=True)
    default_model: str = Field(default="", max_length=100)  # 空=使用系统默认
    tools_enabled: list = Field(default_factory=list, sa_column=Column(JSON))
    mcp_config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    max_concurrent_subagents: int = Field(default=3)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
