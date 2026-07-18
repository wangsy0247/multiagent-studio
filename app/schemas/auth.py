"""
认证相关的 Pydantic schemas
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_serializer, field_validator

# username 用作文件系统路径段 (~/.multiagent-studio/users/{username}/)，
# 必须与 harness.config.paths._SAFE_USER_ID_RE 保持一致
USERNAME_PATTERN = r"^[A-Za-z0-9_-]+$"
# 系统兜底目录名，禁止注册
RESERVED_USERNAMES = {"default", "anonymous"}


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=50, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=100)

    @field_validator("username")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v.lower() in RESERVED_USERNAMES:
            raise ValueError(f"用户名 '{v}' 为系统保留名，请更换")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID
    username: str
    role: str
    config_status: str = "created"  # "created" | "exists" | "failed: ..."


class UserResponse(BaseModel):
    id: UUID
    email: str
    username: str
    display_name: str
    role: str
    avatar_url: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

    @field_serializer("created_at")
    def serialize_dt(self, dt: datetime) -> str:
        s = dt.isoformat()
        if dt.tzinfo is None:
            s += "Z"
        return s


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=100)
    avatar_url: Optional[str] = Field(default=None, max_length=500)
