"""
定时任务 API: CRUD、手动触发、执行历史、cron 预览
"""

import logging
import os
import uuid as uuid_mod
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.engine import get_db
from app.models.scheduled_task import ScheduledTask, TaskRun
from app.models.user import User
from app.services import cron_utils

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_JOBS_PER_USER = int(os.getenv("SCHEDULER_MAX_JOBS_PER_USER", "50"))  # 每用户任务数上限（CC 同款 50）

_VALID_MODES = ("single", "team")
_VALID_THREAD_STRATEGIES = ("new", "fixed")


# ── 请求模型 ─────────────────────────────────────────────────


class ScheduledTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    cron_expr: Optional[str] = None  # recurring 任务必填（与 run_at/delay 三选一）
    run_at: Optional[datetime] = None  # 一次性任务绝对时间；naive 按 timezone 解释
    delay: Optional[str] = None  # 一次性任务相对时长 "10m"/"2h"/"1d"（服务器时钟换算，调用方无需知道当前时间）
    timezone: str = cron_utils.DEFAULT_TIMEZONE
    mode: str = "single"
    project_id: Optional[str] = None
    agent_name: Optional[str] = None
    thread_strategy: str = "new"
    thread_id: Optional[uuid_mod.UUID] = None
    expires_at: Optional[datetime] = None
    allow_silent: bool = False  # 静默模式：Agent 回 [SILENT] 时不写会话、不提醒


class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    prompt: Optional[str] = Field(default=None, min_length=1)
    cron_expr: Optional[str] = None
    run_at: Optional[datetime] = None
    delay: Optional[str] = None
    timezone: Optional[str] = None
    mode: Optional[str] = None
    project_id: Optional[str] = None
    agent_name: Optional[str] = None
    thread_strategy: Optional[str] = None
    thread_id: Optional[uuid_mod.UUID] = None
    enabled: Optional[bool] = None
    expires_at: Optional[datetime] = None
    allow_silent: Optional[bool] = None


# ── 校验与辅助 ────────────────────────────────────────────────


def _validate_schedule(
    cron_expr: Optional[str],
    run_at: Optional[datetime],
    delay: Optional[str],
    tz_name: str,
) -> None:
    provided = sum(1 for x in (cron_expr, run_at, delay) if x)
    if provided > 1:
        raise HTTPException(status_code=400, detail="cron_expr（周期）、run_at（一次性）、delay（相对时长）只能提供一个")
    if provided == 0:
        raise HTTPException(status_code=400, detail="cron_expr（周期）、run_at（一次性）、delay（相对时长）必须提供一个")
    if err := cron_utils.validate_timezone_name(tz_name):
        raise HTTPException(status_code=400, detail=err)
    if cron_expr and (err := cron_utils.validate_cron_expr(cron_expr)):
        raise HTTPException(status_code=400, detail=err)
    if delay and (err := cron_utils.parse_delay(delay)[1]):
        raise HTTPException(status_code=400, detail=err)


def _validate_execution(mode: str, thread_strategy: str) -> None:
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode 必须是 {_VALID_MODES} 之一")
    if thread_strategy not in _VALID_THREAD_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"thread_strategy 必须是 {_VALID_THREAD_STRATEGIES} 之一")


async def _get_own_task(task_id: uuid_mod.UUID, current_user: User, db: AsyncSession) -> ScheduledTask:
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return task


# ── 路由 ─────────────────────────────────────────────────────
# 注意: /preview 必须注册在 /{task_id} 之前，否则会被路径参数吞掉


@router.get("/preview")
async def preview_cron(
    cron_expr: str,
    timezone: str = cron_utils.DEFAULT_TIMEZONE,
    count: int = 5,
    current_user: User = Depends(get_current_user),
):
    """预览 cron 表达式未来的触发时间（任务本地时区，不含 jitter）"""
    if err := cron_utils.validate_cron_expr(cron_expr):
        raise HTTPException(status_code=400, detail=err)
    if err := cron_utils.validate_timezone_name(timezone):
        raise HTTPException(status_code=400, detail=err)
    count = max(1, min(count, 20))
    times = cron_utils.preview_run_times(cron_expr, timezone, count)
    return {"cron_expr": cron_expr, "timezone": timezone, "times": [t.isoformat() for t in times]}


@router.get("/unread-count")
async def unread_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """未读执行记录数（总数 + 按任务明细），前端侧边栏红点用"""
    result = await db.execute(
        select(TaskRun.task_id, func.count())
        .join(ScheduledTask, TaskRun.task_id == ScheduledTask.id)
        .where(
            ScheduledTask.user_id == current_user.id,
            TaskRun.seen.is_(False),
            TaskRun.status != "running",
        )
        .group_by(TaskRun.task_id)
    )
    by_task = {str(task_id): count for task_id, count in result.all()}
    return {"total": sum(by_task.values()), "by_task": by_task}


@router.post("")
async def create_task(
    req: ScheduledTaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_schedule(req.cron_expr, req.run_at, req.delay, req.timezone)
    _validate_execution(req.mode, req.thread_strategy)

    count = await db.scalar(
        select(func.count()).select_from(ScheduledTask).where(ScheduledTask.user_id == current_user.id)
    )
    if (count or 0) >= MAX_JOBS_PER_USER:
        raise HTTPException(status_code=400, detail=f"定时任务数量已达上限 ({MAX_JOBS_PER_USER})，请先删除")

    now = cron_utils.utcnow()
    if req.expires_at is not None and cron_utils.to_aware_utc(req.expires_at) <= now:
        raise HTTPException(status_code=400, detail="expires_at 必须是未来时间")

    task_id = uuid_mod.uuid4()
    if req.cron_expr:
        next_run = cron_utils.compute_next_run(req.cron_expr, str(task_id), req.timezone, base_utc=now)
        recurring = True
    else:
        # delay（相对时长）由服务器时钟换算绝对时间，调用方无需知道当前时间
        run_at_input = now + cron_utils.parse_delay(req.delay)[0] if req.delay else req.run_at
        next_run = cron_utils.compute_oneshot_next(run_at_input, req.timezone, str(task_id))
        recurring = False
        if next_run <= now:
            raise HTTPException(status_code=400, detail="run_at 必须是未来时间")

    task = ScheduledTask(
        id=task_id,
        user_id=current_user.id,
        name=req.name,
        prompt=req.prompt,
        cron_expr=req.cron_expr,
        recurring=recurring,
        timezone=req.timezone,
        next_run_at=cron_utils.to_naive_utc(next_run),
        expires_at=cron_utils.to_naive_utc(cron_utils.to_aware_utc(req.expires_at)) if req.expires_at else None,
        mode=req.mode,
        project_id=req.project_id,
        agent_name=req.agent_name,
        thread_strategy=req.thread_strategy,
        thread_id=req.thread_id,
        allow_silent=req.allow_silent,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.get("")
async def list_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ScheduledTask)
        .where(ScheduledTask.user_id == current_user.id)
        .order_by(ScheduledTask.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{task_id}")
async def get_task(
    task_id: uuid_mod.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_own_task(task_id, current_user, db)


@router.patch("/{task_id}")
async def update_task(
    task_id: uuid_mod.UUID,
    req: ScheduledTaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_own_task(task_id, current_user, db)
    data = req.model_dump(exclude_unset=True)
    if not data:
        return task

    # 调度配置校验：提供 run_at/delay 表示切换为一次性任务（cron_expr 将被清空），
    # 否则按周期任务校验 cron_expr
    new_tz = data.get("timezone", task.timezone)
    if err := cron_utils.validate_timezone_name(new_tz):
        raise HTTPException(status_code=400, detail=err)
    if "delay" in data and (err := cron_utils.parse_delay(data["delay"])[1]):
        raise HTTPException(status_code=400, detail=err)
    if "run_at" not in data and "delay" not in data and ("cron_expr" in data or "timezone" in data):
        new_cron = data.get("cron_expr", task.cron_expr)
        if not new_cron:
            raise HTTPException(status_code=400, detail="周期任务必须提供 cron_expr")
        if err := cron_utils.validate_cron_expr(new_cron):
            raise HTTPException(status_code=400, detail=err)
    _validate_execution(data.get("mode", task.mode), data.get("thread_strategy", task.thread_strategy))

    now = cron_utils.utcnow()
    for field, value in data.items():
        if field in ("run_at", "delay"):
            continue  # 虚拟字段，不是模型列，仅在下面用于重算 next_run_at
        setattr(task, field, value)
    task.updated_at = cron_utils.to_naive_utc(now)

    # 调度字段变化 → 重算 next_run_at（从当前时刻起算）
    if "cron_expr" in data and task.cron_expr:
        task.recurring = True
        task.next_run_at = cron_utils.to_naive_utc(
            cron_utils.compute_next_run(task.cron_expr, str(task.id), task.timezone, base_utc=now)
        )
    elif "run_at" in data or "delay" in data:
        task.recurring = False
        task.cron_expr = None
        # delay（相对时长）由服务器时钟换算绝对时间，调用方无需知道当前时间
        run_at_input = (
            now + cron_utils.parse_delay(data["delay"])[0] if "delay" in data else data["run_at"]
        )
        next_run = cron_utils.compute_oneshot_next(run_at_input, task.timezone, str(task.id))
        if next_run <= now:
            raise HTTPException(status_code=400, detail="run_at 必须是未来时间")
        task.next_run_at = cron_utils.to_naive_utc(next_run)
    elif "timezone" in data and task.recurring and task.cron_expr:
        task.next_run_at = cron_utils.to_naive_utc(
            cron_utils.compute_next_run(task.cron_expr, str(task.id), task.timezone, base_utc=now)
        )

    # 从禁用恢复时 next_run_at 已过期 → 重算，避免恢复瞬间触发 misfire
    if data.get("enabled") is True and task.recurring and task.cron_expr and (
        task.next_run_at is None or cron_utils.to_aware_utc(task.next_run_at) <= now
    ):
        task.next_run_at = cron_utils.to_naive_utc(
            cron_utils.compute_next_run(task.cron_expr, str(task.id), task.timezone, base_utc=now)
        )

    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/{task_id}")
async def delete_task(
    task_id: uuid_mod.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_own_task(task_id, current_user, db)
    # 集合级删除，显式按依赖顺序：先子表(task_runs)后父表。
    # 不能依赖 ORM 的删除排序（两模型间无 relationship），Postgres 会报 FK 违规
    await db.execute(sa_delete(TaskRun).where(TaskRun.task_id == task.id))
    await db.delete(task)
    await db.commit()
    return {"ok": True}


@router.post("/{task_id}/trigger")
async def trigger_task(
    task_id: uuid_mod.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """立即触发一次：把 next_run_at 拨到当前时间，由下一 tick 拾取执行"""
    task = await _get_own_task(task_id, current_user, db)
    now = cron_utils.utcnow()
    if task.expires_at is not None and cron_utils.to_aware_utc(task.expires_at) <= now:
        raise HTTPException(status_code=400, detail="任务已过期，无法触发")
    task.next_run_at = cron_utils.to_naive_utc(now)
    task.enabled = True
    task.updated_at = cron_utils.to_naive_utc(now)
    db.add(task)
    await db.commit()
    return {"ok": True, "next_run_at": task.next_run_at.isoformat()}


@router.get("/{task_id}/runs")
async def list_runs(
    task_id: uuid_mod.UUID,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_task(task_id, current_user, db)
    result = await db.execute(
        select(TaskRun)
        .where(TaskRun.task_id == task_id)
        .order_by(TaskRun.started_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return result.scalars().all()


@router.post("/{task_id}/runs/mark-seen")
async def mark_runs_seen(
    task_id: uuid_mod.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把该任务的执行记录全部标记为已读（用户展开历史时调用）

    排除 running 状态的记录：进行中的 run 完成后才应产生未读。
    """
    await _get_own_task(task_id, current_user, db)
    result = await db.execute(
        sa_update(TaskRun)
        .where(
            TaskRun.task_id == task_id,
            TaskRun.seen.is_(False),
            TaskRun.status != "running",
        )
        .values(seen=True)
    )
    await db.commit()
    return {"ok": True, "marked": result.rowcount}
