"""
会话相关的 Pydantic schemas
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer


class ThreadCreate(BaseModel):
    title: str = Field(default="新会话", max_length=200)
    preset_type: Optional[str] = Field(default=None, max_length=50)
    execution_graph: Optional[dict] = None
    # ── Agent Team 字段 ──
    project_id: Optional[str] = Field(default=None, max_length=50)
    agent_name: Optional[str] = Field(default=None, max_length=100)
    mode: str = Field(default="single", max_length=20)  # "single" | "team"


class ThreadResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    status: str
    preset_type: Optional[str] = None
    execution_graph: Optional[dict] = None
    extra_metadata: dict = {}
    is_archived: bool
    # ── Agent Team 字段 ──
    project_id: Optional[str] = None
    agent_name: Optional[str] = None
    mode: str = "single"
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @field_serializer("created_at", "updated_at")
    def serialize_dt(self, dt: datetime) -> str:
        """确保前端 new Date() 能正确解析: naive datetime 追加 Z 后缀"""
        s = dt.isoformat()
        if dt.tzinfo is None:
            s += "Z"
        return s


class ThreadListResponse(BaseModel):
    threads: list[ThreadResponse]
    total: int
    page: int
    page_size: int


class ThreadUpdateTitle(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ThreadUpdateGraph(BaseModel):
    execution_graph: dict


class MessageResponse(BaseModel):
    id: UUID
    thread_id: UUID
    role: str
    content: str
    msg_type: str
    extra_metadata: dict
    created_at: datetime
    token_count: int

    class Config:
        from_attributes = True

    @field_serializer("created_at")
    def serialize_dt(self, dt: datetime) -> str:
        s = dt.isoformat()
        if dt.tzinfo is None:
            s += "Z"
        return s
