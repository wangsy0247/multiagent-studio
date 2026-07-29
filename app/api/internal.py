"""
内部服务间 API — 供 Harness 内置工具调用（cron 定时任务、session_search 会话搜索）

认证: X-Internal-Token 共享密钥（INTERNAL_API_TOKEN 环境变量，app 与 harness 需一致）。
身份: harness 侧的用户标识统一为 username，这里解析为 User 后复用面向用户的路由逻辑。
通过本接口创建的任务 created_by="agent"，界面上会有标识。
"""

import logging
import os
import uuid as uuid_mod

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskUpdate,
    create_task,
    delete_task,
    list_tasks,
    trigger_task,
    update_task,
)
from app.db.engine import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


async def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    """服务间共享密钥校验（每次调用时读取，支持运行时更新配置）"""
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="内部接口未启用（未配置 INTERNAL_API_TOKEN）")
    if not x_internal_token or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="内部接口认证失败")


async def _resolve_user(username: str, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail=f"用户不存在: {username}")
    return user


class InternalTaskCreate(ScheduledTaskCreate):
    username: str


@router.post("/scheduled-tasks", dependencies=[Depends(require_internal_token)])
async def create_task_internal(
    req: InternalTaskCreate,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user(req.username, db)
    task = await create_task(req, current_user=user, db=db)
    task.created_by = "agent"
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info("[internal] Agent 为用户 %s 创建定时任务: %s(%s)", req.username, task.name, task.id)
    return task


@router.get("/scheduled-tasks", dependencies=[Depends(require_internal_token)])
async def list_tasks_internal(
    username: str,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user(username, db)
    return await list_tasks(current_user=user, db=db)


@router.patch("/scheduled-tasks/{task_id}", dependencies=[Depends(require_internal_token)])
async def update_task_internal(
    task_id: uuid_mod.UUID,
    req: ScheduledTaskUpdate,
    username: str,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user(username, db)
    return await update_task(task_id, req, current_user=user, db=db)


@router.delete("/scheduled-tasks/{task_id}", dependencies=[Depends(require_internal_token)])
async def delete_task_internal(
    task_id: uuid_mod.UUID,
    username: str,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user(username, db)
    return await delete_task(task_id, current_user=user, db=db)


@router.post("/scheduled-tasks/{task_id}/trigger", dependencies=[Depends(require_internal_token)])
async def trigger_task_internal(
    task_id: uuid_mod.UUID,
    username: str,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user(username, db)
    return await trigger_task(task_id, current_user=user, db=db)


# ── session_search（Agent 搜索历史会话消息）─────────────────────────────────


class SessionSearchRequest(BaseModel):
    username: str
    query: str
    exclude_thread_id: str | None = None  # 通常为当前会话，避免搜到自己
    max_sessions: int = 3


@router.post("/session-search", dependencies=[Depends(require_internal_token)])
async def session_search_internal(
    req: SessionSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.services.session_search import search_messages

    user = await _resolve_user(req.username, db)
    sessions = await search_messages(
        db,
        user_id=user.id,
        query=req.query,
        exclude_thread_id=req.exclude_thread_id,
        max_sessions=req.max_sessions,
    )
    return {"sessions": sessions}
