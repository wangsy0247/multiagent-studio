"""
认证服务: 密码哈希、JWT 签发/验证
"""

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
from jose import jwt

from app.config import get_settings


def _jwt_secret() -> str:
    return get_settings().jwt_secret


def _jwt_algorithm() -> str:
    return get_settings().jwt_algorithm


def _token_expire_minutes() -> int:
    return get_settings().access_token_expire_minutes


def hash_password(password: str) -> str:
    # bcrypt 限制 72 字节，先 SHA256 处理长密码
    if len(password.encode()) > 72:
        password = hashlib.sha256(password.encode()).hexdigest()
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if len(plain_password.encode()) > 72:
        plain_password = hashlib.sha256(plain_password.encode()).hexdigest()
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_access_token(user_id: str, role: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=_token_expire_minutes()))
    payload = {
        "sub": user_id,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_algorithm())


def decode_token(token: str) -> dict:
    return jwt.decode(token, _jwt_secret(), algorithms=[_jwt_algorithm()])
