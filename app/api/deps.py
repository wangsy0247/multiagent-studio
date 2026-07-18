"""
FastAPI 依赖注入: 数据库会话、当前用户验证
"""

import logging
import uuid as uuid_mod

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import jwt, JWTError

from app.config import get_settings
from app.db.engine import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

security = HTTPBearer()


def _parse_uuid(value: str | None) -> uuid_mod.UUID | None:
    """把 JWT sub 字符串转成 UUID 对象 (SQLAlchemy Uuid 列不接受 str 直接比较)。"""
    if not value:
        return None
    try:
        return uuid_mod.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 JWT token 解析当前登录用户"""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[get_settings().jwt_algorithm])
        user_id = _parse_uuid(payload.get("sub"))
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    return user


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """验证管理员权限"""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user


# ── 文件系统 user_id 解析 ────────────────────────────────────────────────
# 文件系统目录 (~/.multiagent-studio/users/{X}/) 统一使用 username。
# JWT sub 是 uuid，必须经 DB 翻译成 username，否则会写出 uuid 目录造成数据分裂。


def extract_jwt_sub(authorization: str | None) -> str | None:
    """从 Authorization: Bearer <token> 的 JWT 中提取 user_id (sub 字段, 即 uuid)。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        token = authorization[7:]
        settings = get_settings()
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload.get("sub")
    except (JWTError, Exception):
        return None


async def resolve_fs_user_id(
    explicit: str | None,
    authorization: str | None,
    db: AsyncSession,
) -> str:
    """解析文件系统使用的 user_id (统一为 username)：

    1. explicit 非空且不是 "default" → 直接使用 (前端传的就是 username)
    2. 否则从 JWT 提取 sub (uuid)，查 DB 翻译成 username
    3. 都失败 → "default"
    """
    if explicit and explicit != "default":
        return explicit
    uid = _parse_uuid(extract_jwt_sub(authorization))
    if uid is not None:
        result = await db.execute(select(User).where(User.id == uid))
        user = result.scalar_one_or_none()
        if user is not None:
            logger.info("[resolve_fs_user_id] JWT 兜底 → username=%s", user.username)
            return user.username
        logger.warning("[resolve_fs_user_id] JWT sub=%s 对应用户不存在，回退 default", uid)
    return "default"
