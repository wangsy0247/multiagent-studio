"""
SQLModel 消息模型
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import JSON, Field, SQLModel, Column, Text


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    thread_id: uuid.UUID = Field(foreign_key="threads.id", index=True)
    role: str = Field(max_length=20)  # human|ai|tool|subagent|system
    content: str = Field(default="", sa_column=Column(Text))
    msg_type: str = Field(default="text", max_length=30)  # text|tool_call|tool_result|subagent_start|subagent_end|error
    extra_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    parent_id: Optional[uuid.UUID] = Field(default=None, foreign_key="messages.id")
    token_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
