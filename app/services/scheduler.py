"""
定时任务调度器 — 轮询 DB 的哑调度循环（参考 hermes-agent / Claude Code 设计）

高可用设计:
- next_run_at 持久化在 DB，进程重启后自然恢复，无内存调度状态
- 执行前 CAS 推进 next_run_at（at-most-once，崩溃不重跑，多实例安全）
- misfire 快进不补跑，避免停机重启后的补跑风暴
- 启动时把中断的 TaskRun 标记为 interrupted（崩溃恢复）
- 优雅关闭：停止认领新任务，等待在途执行完成

高并发设计:
- 调度与执行解耦：tick 只负责认领，执行在独立 asyncio task 中
- 信号量限制并发执行数，单 tick 认领数量上限
- fixed 策略的 thread 正在被交互使用时推迟触发（对应 CC 的"Agent 空闲才交付"）
"""

import asyncio
import json
import logging
import os
import uuid as uuid_mod
from datetime import datetime
from typing import Optional

from sqlalchemy import case, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import async_session
from app.models.message import Message
from app.models.scheduled_task import ScheduledTask, TaskRun
from app.models.thread import Thread
from app.models.user import User
from app.services import cron_utils
from app.services.harness_client import get_harness_client

logger = logging.getLogger(__name__)

# ── 配置（环境变量可调） ─────────────────────────────────────
TICK_INTERVAL = float(os.getenv("SCHEDULER_TICK_INTERVAL", "15"))  # tick 间隔（秒）
MAX_CONCURRENCY = int(os.getenv("SCHEDULER_MAX_CONCURRENCY", "20"))  # 并发执行上限
MAX_CLAIM_PER_TICK = int(os.getenv("SCHEDULER_MAX_CLAIM_PER_TICK", "100"))  # 单 tick 认领上限
RUN_TIMEOUT = float(os.getenv("SCHEDULER_RUN_TIMEOUT", "14400"))  # 单次执行总时长硬上限（秒，默认 4h）
INACTIVITY_TIMEOUT = float(os.getenv("SCHEDULER_INACTIVITY_TIMEOUT", "600"))  # 无 SSE 活动判定挂起（秒）
SHUTDOWN_GRACE = float(os.getenv("SCHEDULER_SHUTDOWN_GRACE", "30"))  # 关闭时等待在途执行的秒数
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# 静默模式提示：allow_silent 任务的 prompt 追加此说明（hermes 同款 [SILENT] 约定）
_SILENT_HINT = (
    "\n\n[系统说明] 本任务为无人值守定时执行。执行后如果没有值得告知用户的结果"
    "（如无异常、无新增内容、一切正常），请只回复 [SILENT]，不要输出任何其他内容。"
)

_EVENT_ROLE_MAP = {
    "tool_call": "tool",
    "tool_result": "tool",
    "subagent_start": "subagent",
    "subagent_end": "subagent",
    "error": "system",
}
_PERSIST_EVENT_TYPES = set(_EVENT_ROLE_MAP)


class SchedulerService:
    """调度器：后台 tick 循环 + 有界并发执行池"""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._inflight: set[asyncio.Task] = set()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── 生命周期 ─────────────────────────────────────────────

    async def start(self) -> None:
        if not SCHEDULER_ENABLED:
            logger.info("[scheduler] SCHEDULER_ENABLED=false，调度器未启动")
            return
        await self._recover_interrupted_runs()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler-loop")
        logger.info(
            "[scheduler] 已启动: tick=%ss, 并发上限=%d, 单tick认领上限=%d, 执行超时=%ss",
            TICK_INTERVAL, MAX_CONCURRENCY, MAX_CLAIM_PER_TICK, RUN_TIMEOUT,
        )

    async def shutdown(self) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if self._inflight:
            logger.info("[scheduler] 等待 %d 个在途执行完成（最多 %ss）...", len(self._inflight), SHUTDOWN_GRACE)
            done, pending = await asyncio.wait(self._inflight, timeout=SHUTDOWN_GRACE)
            for t in pending:
                t.cancel()  # 未完成的 run 记录保持 running，下次启动由恢复逻辑标记 interrupted
            if pending:
                logger.warning("[scheduler] %d 个执行被强制取消", len(pending))
        logger.info("[scheduler] 已停止")

    async def _recover_interrupted_runs(self) -> None:
        """启动时把上次进程退出时遗留在 running 状态的 run 标记为 interrupted"""
        async with async_session() as db:
            result = await db.execute(
                sa_update(TaskRun)
                .where(TaskRun.status == "running")
                .values(status="interrupted", error="服务重启，执行中断",
                        finished_at=cron_utils.to_naive_utc(cron_utils.utcnow()))
            )
            if result.rowcount:
                await db.commit()
                logger.warning("[scheduler] 恢复 %d 个中断的执行记录", result.rowcount)

            # Thread 状态同样需要恢复: 进程退出时泵任务死亡, threads 表
            # 永久卡 running — 前端永远显示运行中, 绑定它的 fixed 定时任务
            # 每次 tick 都被当作"忙"跳过, 永久停摆
            from app.models.thread import Thread
            t_result = await db.execute(
                sa_update(Thread)
                .where(Thread.status == "running")
                .values(status="idle")
            )
            if t_result.rowcount:
                await db.commit()
                logger.warning("[scheduler] 恢复 %d 个卡在 running 的会话 → idle", t_result.rowcount)

    # ── tick 循环 ────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stopping:
            claimed = 0
            try:
                claimed = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[scheduler] tick 异常（不影响后续 tick）")
            # 认领取满说明有积压（如整点高峰），立即连续 tick 直到排空；
            # 否则正常休眠。全部因忙碌/竞争未认领时也走休眠，避免空转。
            if claimed < MAX_CLAIM_PER_TICK:
                await asyncio.sleep(TICK_INTERVAL)

    async def _tick(self) -> int:
        """扫描到期任务并批量认领派发，返回本次认领数"""
        now = cron_utils.utcnow()
        async with async_session() as db:
            result = await db.execute(
                select(ScheduledTask)
                .where(
                    ScheduledTask.enabled.is_(True),
                    ScheduledTask.next_run_at.is_not(None),
                    ScheduledTask.next_run_at <= cron_utils.to_naive_utc(now),
                )
                .limit(MAX_CLAIM_PER_TICK)
            )
            due_tasks = result.scalars().all()
            if not due_tasks:
                return 0

            # fixed 策略且绑定的 thread 正在交互执行 → 本轮不碰（批量检查，一次查询）
            busy_thread_ids = await self._busy_thread_ids(db, due_tasks)

            # 计算每个任务的触发决策（纯函数，无 DB 往返）；单个失败不影响其他任务
            plans: list[tuple[ScheduledTask, cron_utils.FirePlan]] = []
            for task in due_tasks:
                if self._stopping:
                    break
                if task.thread_strategy == "fixed" and task.thread_id in busy_thread_ids:
                    continue
                try:
                    plan = cron_utils.resolve_fire_plan(
                        recurring=task.recurring,
                        cron_expr=task.cron_expr,
                        tz_name=task.timezone,
                        task_id=str(task.id),
                        next_run_at=cron_utils.to_aware_utc(task.next_run_at),
                        expires_at=cron_utils.to_aware_utc(task.expires_at) if task.expires_at else None,
                        now=now,
                    )
                except Exception:
                    logger.exception("[scheduler] 计算任务 %s 触发决策失败，跳过", task.id)
                    continue
                plans.append((task, plan))

            # 批量 CAS 认领：一次 DB 往返，RETURNING 返回实际认领到的任务 id
            claimed_ids = await self._claim_batch(db, plans, now)

            for task, plan in plans:
                if task.id not in claimed_ids:
                    continue  # 被其他实例/请求抢先，放弃
                if plan.action == "fire":
                    self._dispatch(task.id)
                elif plan.action == "skip":
                    logger.info("[scheduler] 任务 %s(%s) 本次触发跳过: %s", task.name, task.id, plan.last_status)
            return len(claimed_ids)

    @staticmethod
    async def _busy_thread_ids(db: AsyncSession, tasks: list[ScheduledTask]) -> set[uuid_mod.UUID]:
        """一次性查出所有 fixed 策略绑定且正在运行的 thread id"""
        thread_ids = [t.thread_id for t in tasks if t.thread_strategy == "fixed" and t.thread_id is not None]
        if not thread_ids:
            return set()
        result = await db.execute(
            select(Thread.id).where(Thread.id.in_(thread_ids), Thread.status == "running")
        )
        return set(result.scalars().all())

    async def _claim_batch(
        self,
        db: AsyncSession,
        plans: list[tuple[ScheduledTask, cron_utils.FirePlan]],
        now: datetime,
    ) -> set[uuid_mod.UUID]:
        """批量 CAS 认领（单条 UPDATE + CASE WHEN + RETURNING）

        一次 round-trip 完成整批"先推进 next_run_at"操作；CAS 条件不变：
        仅当 next_run_at 未被其他实例改动时才生效（at-most-once）。
        用 CASE WHEN 而非 UPDATE...FROM(VALUES)，兼容 SQLite 与 Postgres。
        """
        if not plans:
            return set()

        def _by_id(fn):
            """把 plans 转成 {task_id: value} 的 CASE 映射"""
            return {task.id: fn(task, plan) for task, plan in plans}

        stmt = (
            sa_update(ScheduledTask)
            .where(
                ScheduledTask.id.in_([task.id for task, _ in plans]),
                # CAS：仅当 next_run_at 未被其他实例改动时才认领
                ScheduledTask.next_run_at == case(
                    _by_id(lambda t, p: t.next_run_at), value=ScheduledTask.id
                ),
            )
            .values(
                next_run_at=case(
                    _by_id(lambda t, p: cron_utils.to_naive_utc(p.next_run_at) if p.next_run_at else None),
                    value=ScheduledTask.id,
                ),
                enabled=case(_by_id(lambda t, p: p.enabled), value=ScheduledTask.id),
                # plan.last_status 为 None 时保留原值
                last_status=func.coalesce(
                    case(_by_id(lambda t, p: p.last_status), value=ScheduledTask.id),
                    ScheduledTask.last_status,
                ),
                updated_at=cron_utils.to_naive_utc(now),
            )
            .returning(ScheduledTask.id)
        )
        result = await db.execute(stmt)
        claimed = {row[0] for row in result.all()}
        await db.commit()
        return claimed

    # ── 执行派发 ─────────────────────────────────────────────

    def _dispatch(self, task_id: uuid_mod.UUID) -> None:
        t = asyncio.create_task(self._run_guarded(task_id), name=f"scheduled-task-{task_id}")
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    async def _run_guarded(self, task_id: uuid_mod.UUID) -> None:
        async with self._semaphore:
            try:
                await run_scheduled_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[scheduler] 任务 %s 执行异常", task_id)


# ── 单次执行 ─────────────────────────────────────────────────


async def run_scheduled_task(task_id: uuid_mod.UUID) -> None:
    """执行一个定时任务：建/复用 thread → 调 harness → 消费 SSE 并持久化 → 写执行记录"""
    async with async_session() as db:
        task = await db.get(ScheduledTask, task_id)
        # 不检查 enabled：一次性任务认领时已被置为 disabled（fire-then-disable），
        # 若此处因 disabled 拒绝执行，一次性任务将永远不会运行。
        # 认领→执行之间用户主动停用属于毫秒级竞态，按 at-most-once 语义接受。
        if task is None:
            return
        now = cron_utils.utcnow()
        if task.expires_at is not None and cron_utils.to_aware_utc(task.expires_at) <= now:
            task.enabled = False
            task.last_status = "expired"
            task.updated_at = cron_utils.to_naive_utc(now)
            await db.commit()
            return

        user = await db.get(User, task.user_id)
        if user is None:
            task.enabled = False
            task.last_status = "error"
            task.last_error = "任务所属用户不存在"
            await db.commit()
            return

        thread = await _resolve_thread(db, task)
        thread.status = "running"
        db.add(thread)

        run = TaskRun(
            task_id=task.id,
            thread_id=thread.id,
            status="running",
            started_at=cron_utils.to_naive_utc(now),
        )
        db.add(run)
        task.last_run_at = cron_utils.to_naive_utc(now)
        await db.commit()

        status, error_text, summary = "success", None, None
        silent = False
        try:
            result = await asyncio.wait_for(
                _consume_stream(db, task, thread, user.username),
                timeout=RUN_TIMEOUT,  # 总时长硬上限兜底；主要超时语义是 inactivity
            )
            silent = result is None  # None = 静默运行（[SILENT]），未写会话消息
            if not silent and result.strip():
                summary = result[:500]
            thread.status = "finished"
        except InactivityTimeoutError as e:
            status, error_text = "timeout", str(e)[:2000]
            thread.status = "error"
        except asyncio.TimeoutError:
            status, error_text = "timeout", f"超过总时长上限 ({RUN_TIMEOUT / 3600:.0f}h)"
            thread.status = "error"
        except Exception as e:
            status, error_text = "error", str(e)[:2000]
            thread.status = "error"
            logger.exception("[scheduler] 任务 %s 执行失败", task_id)

        run.status = status
        run.error = error_text
        run.summary = summary
        run.seen = silent  # 静默运行不产生未读提醒
        run.finished_at = cron_utils.to_naive_utc(cron_utils.utcnow())
        task.last_status = status
        task.last_error = error_text
        db.add_all([run, task, thread])
        await db.commit()
        logger.info("[scheduler] 任务 %s(%s) 执行结束: %s", task.name, task_id, status)


async def _resolve_thread(db: AsyncSession, task: ScheduledTask) -> Thread:
    """按 thread_strategy 解析目标 thread：fixed 复用（被删则重建并回写），new 每次新建"""
    thread: Optional[Thread] = None
    if task.thread_strategy == "fixed" and task.thread_id is not None:
        thread = await db.get(Thread, task.thread_id)
    if thread is None:
        thread = Thread(
            user_id=task.user_id,
            title=f"[定时] {task.name}",
            status="running",
            mode=task.mode,
            project_id=task.project_id,
            agent_name=task.agent_name,
        )
        db.add(thread)
        await db.flush()
        if task.thread_strategy == "fixed":
            task.thread_id = thread.id
    return thread


async def _consume_stream(
    db: AsyncSession,
    task: ScheduledTask,
    thread: Thread,
    username: str,
) -> Optional[str]:
    """消费 harness SSE 流并持久化关键事件（精简自 app/api/execute.py）。

    返回累积的 AI 文本；返回 None 表示静默运行（Agent 回复 [SILENT]），
    此时缓冲的消息不落库、调用方不产生未读。

    - allow_silent 任务：消息先缓冲，确认非静默后统一落库（防刷屏）；
      提示词注入 [SILENT] 约定，但会话中持久化的仍是原始 prompt
    - inactivity 超时：每个 SSE 事件重置计时，超过 INACTIVITY_TIMEOUT 无事件判定挂起
    """
    harness = get_harness_client()
    accumulated = ""
    buffer: list[Message] = []  # allow_silent 时缓冲，确认非静默后统一落库
    last_message: Optional[Message] = None

    async def emit(msg: Message) -> None:
        if task.allow_silent:
            buffer.append(msg)
        else:
            db.add(msg)
            await db.commit()

    await emit(Message(
        thread_id=thread.id,
        role="human",
        content=task.prompt,
        msg_type="text",
        extra_metadata={"source": "scheduled_task", "task_id": str(task.id)},
        token_count=0,
    ))

    outgoing_prompt = task.prompt + (_SILENT_HINT if task.allow_silent else "")
    agen = harness.stream_execute(
        thread_id=str(thread.id),
        user_id=username,  # 文件系统目录统一使用 username
        message=outgoing_prompt,
        mode=task.mode,
        project_id=task.project_id,
        agent_name=task.agent_name,
        unattended=True,  # 定时执行为无人值守：禁用 cron/澄清等交互工具
    )
    try:
        while True:
            try:
                event_json = await asyncio.wait_for(anext(agen), timeout=INACTIVITY_TIMEOUT)
            except asyncio.TimeoutError:
                raise InactivityTimeoutError(
                    f"超过 {INACTIVITY_TIMEOUT:.0f}s 无任何执行活动，判定为挂起"
                ) from None
            except StopAsyncIteration:
                break

            try:
                event = json.loads(event_json) if isinstance(event_json, str) else event_json
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type", "")

            if event_type == "message" and event.get("content"):
                accumulated += event["content"]
            elif event_type in _PERSIST_EVENT_TYPES:
                msg = Message(
                    thread_id=thread.id,
                    role=_EVENT_ROLE_MAP[event_type],
                    content=event.get("content") or event.get("tool_result") or event.get("instruction") or "",
                    msg_type=event_type,
                    extra_metadata=event,
                    token_count=0,
                )
                await emit(msg)
                last_message = msg
                if event_type == "error":
                    raise ScheduledRunError(event.get("content") or "harness 返回错误")
            elif event_type == "token_usage" and last_message is not None:
                try:
                    tokens = event.get("tokens", {})
                    total = tokens.get("total_tokens", 0) if isinstance(tokens, dict) else 0
                    if task.allow_silent:
                        last_message.token_count = total  # 内存对象，随 buffer 一起落库
                    else:
                        await db.execute(
                            sa_update(Message).where(Message.id == last_message.id).values(token_count=total)
                        )
                        await db.commit()
                except Exception:
                    pass  # best-effort
            elif event_type == "finished":
                break
            # title_update 忽略：定时任务保留 "[定时] {name}" 标题便于识别
            # clarification 忽略：harness 侧无人值守闸门已把 ask_clarification 转为自行决策
    finally:
        await agen.aclose()

    if task.allow_silent:
        if accumulated.strip().upper() == "[SILENT]":
            logger.info("[scheduler] 任务 %s(%s) 静默完成（[SILENT]）", task.name, task.id)
            return None
        if accumulated.strip():
            buffer.append(Message(
                thread_id=thread.id,
                role="ai",
                content=accumulated,
                msg_type="message",
                extra_metadata={"source": "scheduled_task"},
                token_count=0,
            ))
        db.add_all(buffer)
        await db.commit()
    elif accumulated.strip():
        db.add(Message(
            thread_id=thread.id,
            role="ai",
            content=accumulated,
            msg_type="message",
            extra_metadata={"source": "scheduled_task"},
            token_count=0,
        ))
        await db.commit()
    return accumulated


class ScheduledRunError(Exception):
    """harness 执行流中返回的业务错误"""


class InactivityTimeoutError(Exception):
    """INACTIVITY_TIMEOUT 秒内无任何 SSE 事件，判定执行挂起"""


# ── 单例 ─────────────────────────────────────────────────────

_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler
