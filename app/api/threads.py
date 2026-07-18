"""
会话 API 路由: Thread 的 CRUD、消息列表
"""

import logging
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.db.engine import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.thread import Thread
from app.models.message import Message
from app.schemas.thread import (
    ThreadCreate, ThreadResponse, ThreadListResponse,
    ThreadUpdateTitle, ThreadUpdateGraph, MessageResponse,
)
from app.services.harness_client import get_harness_client
from harness.config.paths import get_paths

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=ThreadListResponse)
async def list_threads(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 总数
    count_result = await db.execute(
        select(func.count(Thread.id)).where(
            Thread.user_id == current_user.id, Thread.is_archived == False
        )
    )
    total = count_result.scalar()

    # 分页查询
    result = await db.execute(
        select(Thread)
        .where(Thread.user_id == current_user.id, Thread.is_archived == False)
        .order_by(desc(Thread.updated_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    threads = result.scalars().all()

    return ThreadListResponse(
        threads=[ThreadResponse.model_validate(t) for t in threads],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=ThreadResponse, status_code=201)
async def create_thread(
    req: ThreadCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    thread = Thread(
        user_id=current_user.id,
        title=req.title,
        preset_type=req.preset_type,
        execution_graph=req.execution_graph,
        project_id=req.project_id,
        agent_name=req.agent_name,
        mode=req.mode,
    )
    db.add(thread)
    await db.flush()
    await db.refresh(thread)
    await db.commit()
    logger.info(f"创建会话: {thread.id} by user {current_user.username}")
    return ThreadResponse.model_validate(thread)


@router.get("/by-project/{project_id}", response_model=ThreadListResponse)
async def list_threads_by_project(
    project_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出属于某个项目的所有会话."""
    count_result = await db.execute(
        select(func.count(Thread.id)).where(
            Thread.user_id == current_user.id,
            Thread.is_archived == False,
            Thread.project_id == project_id,
        )
    )
    total = count_result.scalar()

    result = await db.execute(
        select(Thread)
        .where(
            Thread.user_id == current_user.id,
            Thread.is_archived == False,
            Thread.project_id == project_id,
        )
        .order_by(desc(Thread.updated_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    threads = result.scalars().all()

    return ThreadListResponse(
        threads=[ThreadResponse.model_validate(t) for t in threads],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return ThreadResponse.model_validate(thread)


@router.delete("/{thread_id}")
async def delete_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    thread_id_str = str(thread_id)
    # 文件系统目录统一使用 username (~/.multiagent-studio/users/{username}/)
    user_id_str = current_user.username

    # 1) 归档 — App DB 软删除
    thread.is_archived = True
    thread.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()
    await db.commit()

    # 2) 直接删除线程工作目录
    #    路径: {data_root}/users/{user_id}/threads/{thread_id}/
    paths = get_paths()
    thread_dir = paths.thread_dir(thread_id_str, user_id=user_id_str)
    if thread_dir.exists():
        paths.delete_thread_dir(thread_id_str, user_id=user_id_str)
        logger.info("已删除线程目录: %s", thread_dir)
    else:
        logger.info("线程目录不存在(可能从未执行): %s", thread_dir)

    # 3) 通知 Harness 清理 LangGraph checkpoint (best-effort)
    try:
        harness = get_harness_client()
        await harness.delete_thread(thread_id_str, user_id=user_id_str)
    except Exception as exc:
        logger.warning("Harness checkpoint 清理失败 (可忽略): %s", exc)

    return {"success": True, "message": "会话已删除"}


@router.patch("/{thread_id}/title", response_model=ThreadResponse)
async def update_title(
    thread_id: UUID,
    req: ThreadUpdateTitle,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    thread.title = req.title
    thread.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()
    await db.refresh(thread)
    await db.commit()
    return ThreadResponse.model_validate(thread)


@router.patch("/{thread_id}/graph", response_model=ThreadResponse)
async def update_graph(
    thread_id: UUID,
    req: ThreadUpdateGraph,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    thread.execution_graph = req.execution_graph
    thread.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()
    await db.refresh(thread)
    await db.commit()
    return ThreadResponse.model_validate(thread)


@router.get("/{thread_id}/messages")
async def list_messages(
    thread_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 验证会话归属
    result = await db.execute(
        select(Thread).where(Thread.id == thread_id, Thread.user_id == current_user.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 查询消息
    msgs_result = await db.execute(
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(Message.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    messages = msgs_result.scalars().all()

    # 总数
    count_result = await db.execute(
        select(func.count(Message.id)).where(Message.thread_id == thread_id)
    )
    total = count_result.scalar()

    return {
        "messages": [MessageResponse.model_validate(m) for m in messages],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
