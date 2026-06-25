"""
监控 API 路由: Trace、Token 使用统计 (代理到 Harness/Langfuse)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.models.user import User
from app.services.harness_client import get_harness_client, HarnessUnavailableError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/traces/{thread_id}")
async def get_trace(
    thread_id: str,
    current_user: User = Depends(get_current_user),
):
    """获取会话的 Trace 详情"""
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
    """获取 Token 使用统计"""
    harness = get_harness_client()
    params = {}
    if user_id:
        params["user_id"] = user_id
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
