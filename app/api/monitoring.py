"""
监控 API 路由: Trace、Token 使用统计 (代理到 Harness/Langfuse)
"""

import logging
import uuid as uuid_mod
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.engine import get_db
from app.models.thread import Thread
from app.models.user import User
from app.services.harness_client import get_harness_client, HarnessUnavailableError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/traces/{thread_id}")
async def get_trace(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取会话的 Trace 详情 (含归属校验 — trace 含完整对话内容)"""
    try:
        thread_uuid = uuid_mod.UUID(thread_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid thread_id")
    result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    harness = get_harness_client()
    try:
        return await harness.get_trace(thread_id)
    except HarnessUnavailableError:
        raise HTTPException(status_code=503, detail="Harness 服务不可用")


@router.get("/token-usage")
async def get_token_usage(
    user_id: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """获取 Token 使用统计 (强制限定为当前用户, user_id 参数不可越权)"""
    harness = get_harness_client()
    params = {"user_id": current_user.username}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if model:
        params["model"] = model

    try:
        return await harness.get_token_usage(**params)
    except HarnessUnavailableError:
        raise HTTPException(status_code=503, detail="Harness 服务不可用")
