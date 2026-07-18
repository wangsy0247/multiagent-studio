"""
认证 API 路由: 注册、登录、个人信息
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.auth import (
    RegisterRequest, LoginRequest, TokenResponse, UserResponse, UpdateProfileRequest,
)
from app.services.auth_service import hash_password, verify_password, create_access_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # 检查邮箱/用户名唯一性
    existing = await db.execute(
        select(User).where((User.email == req.email) | (User.username == req.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="邮箱或用户名已存在")

    user = User(
        email=req.email,
        username=req.username,
        hashed_password=hash_password(req.password),
        display_name=req.display_name or req.username,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    # ── 为新用户创建全局配置 + default agent ──
    # 文件系统目录统一使用 username (~/.multiagent-studio/users/{username}/)
    config_status = "created"
    try:
        from harness.config import create_user_configs
        create_user_configs(user.username)
        logger.info("Created user configs for new user '%s'", user.username)
    except Exception as exc:
        logger.warning("Failed to create user configs for '%s': %s", user.username, exc)
        config_status = f"failed: {exc}"

    token = create_access_token(str(user.id), user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role,
        config_status=config_status,
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")

    # ── 自愈：确保 username 目录下的全局配置 + default agent 存在 (幂等) ──
    # 兼容统一目录标识之前注册的老用户 (其配置在 uuid 目录或从未创建)
    try:
        from harness.config import create_user_configs
        create_user_configs(user.username)
    except Exception as exc:
        logger.warning("Failed to ensure user configs for '%s': %s", user.username, exc)

    token = create_access_token(str(user.id), user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/me", response_model=UserResponse)
async def update_me(
    req: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.display_name is not None:
        current_user.display_name = req.display_name
    if req.avatar_url is not None:
        current_user.avatar_url = req.avatar_url
    db.add(current_user)
    await db.flush()
    await db.refresh(current_user)
    return current_user
