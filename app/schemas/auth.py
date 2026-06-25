"""
认证相关的 Pydantic schemas
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_serializer


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID
    username: str
    role: str


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
