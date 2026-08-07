"""
执行 API 路由: 代理 Harness 的 SSE 执行、停止、状态查询、断线续传

断线续传 (Phase 3):
- execute / respond 的 Harness 事件流由后台泵任务 (_pump_run) 消费,
  独立于客户端 HTTP 连接 — 页面刷新/断网不影响事件生产与落库;
- 每条事件经 StreamHub 分配单调递增序号 (每次运行从 1 重置),
  SSE 输出帧为标准格式 "id: <seq>\\ndata: {...}\\n\\n";
- resume 端点从事件环形缓冲补发缺失事件后挂接实时流,
  不可续传 (not_running / gap) 时下发一次性 resync 事件,
  由前端回退到状态轮询。
"""

import asyncio
import json
import logging
import uuid as uuid_mod
from typing import AsyncGenerator, AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import get_db, async_session as _default_session_factory
from app.api.deps import get_current_user
from app.models.user import User
from app.models.thread import Thread
from app.models.message import Message
from app.services.harness_client import get_harness_client, HarnessUnavailableError
from app.services.stream_hub import (
    get_stream_hub, RunStream, Subscription, SUB_OK,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ExecuteRequest(BaseModel):
    thread_id: str
    message: str
    execution_graph: Optional[dict] = None
    plan_mode: bool = False
    files: Optional[list[dict]] = None  # 当前消息附带的上传文件元数据
    # ── Agent Team 扩展字段 ──
    project_id: Optional[str] = None
    agent_name: Optional[str] = None
    mode: str = "single"  # "single" | "team"
    mentions: Optional[list[str]] = None  # @点名成员 — 注入指令请 Lead 优先安排


class ClarificationResponse(BaseModel):
    answer: str


async def _sse_frames(sub: Subscription) -> AsyncGenerator[str, None]:
    """把订阅内容编码为标准 SSE 帧 (id + data 行)。

    订阅失败 (not_running / gap) 时下发一次性 resync 事件并结束,
    前端据此回退到状态轮询。
    """
    if sub.status != SUB_OK:
        yield f"data: {json.dumps({'type': 'resync', 'reason': sub.status})}\n\n"
        return
    hub = get_stream_hub()
    try:
        for seq, payload in sub.missed:
            yield f"id: {seq}\ndata: {payload}\n\n"
        while True:
            item = await sub.queue.get()
            if item is None:  # 结束哨兵 (运行终止 / 消费者被背压丢弃)
                break
            seq, payload = item
            yield f"id: {seq}\ndata: {payload}\n\n"
    finally:
        if sub.run is not None and sub.queue is not None:
            hub.unsubscribe(sub.run, sub.queue)


async def _set_thread_status(db: AsyncSession, thread: Optional[Thread], status: str) -> None:
    if thread is None:
        return
    thread.status = status
    db.add(thread)
    await db.commit()


async def _pump_run(
    thread_id: str,
    thread_uuid,
    rs: RunStream,
    stream_factory,
    session_factory=None,
) -> None:
    """后台泵任务: 消费 Harness SSE 流 → 落库 + 经 StreamHub 广播。

    独立于任何 HTTP 连接运行 (execute/respond 的 StreamingResponse 与
    resume 消费者都只是订阅者), 因此客户端断开不中断事件生产;
    DB 使用自带 session (请求级 session 随响应结束已关闭)。
    """
    hub = get_stream_hub()
    sf = session_factory or _default_session_factory
    async with sf() as db:
        thread_result = await db.execute(select(Thread).where(Thread.id == thread_uuid))
        thread = thread_result.scalar_one_or_none()
        # 立即 commit 释放连接: 泵任务贯穿整个运行周期, 而 SQLite 连接池
        # 只有 1 个连接 (app/db/engine.py) — 若挂着未提交事务, 运行期间
        # 所有其他请求都会在 get_current_user 处等连接 30s 后超时。
        # 后续 db.add() 只在内存暂存, 各 commit 点是短暂事务, 不长占连接。
        await db.commit()
        # 流式事件持久化器 — 中间事件即时 db.add, 终态统一 commit;
        # AI 文本/thinking 按 tool_call 边界切段 (与前端气泡形态一致)
        persister = _StreamPersister(db, thread_uuid)
        try:
            async for event_json in stream_factory():
                try:
                    event = json.loads(event_json) if isinstance(event_json, str) else event_json
                except json.JSONDecodeError:
                    hub.publish(rs, event_json if isinstance(event_json, str)
                                else json.dumps(event_json, default=str))
                    continue

                event_type = event.get("type", "")

                if event_type == "clarification":
                    # 澄清暂停: 后端会正常结束流但不发 finished, 必须立即落库,
                    # 否则 session 关闭回滚导致本轮事件全部丢失
                    await _persist_clarification(db, thread, thread_uuid, event)
                else:
                    persister.handle(event_type, event)

                # 分配序号并广播给所有订阅者 (原客户端 + resume 消费者)
                hub.publish(rs, json.dumps(event, default=str))

                # ── 终态事件: 一次性 commit 所有累积数据 ──
                if event_type == "finished":
                    persister.finalize()
                    await _set_thread_status(db, thread, "finished")
                elif event_type == "error":
                    await _set_thread_status(db, thread, "error")
                elif event_type == "team_error":
                    # team 失败走 team_error 而非 error, 同样要落库终态,
                    # 否则会话列表永远显示 running
                    await _set_thread_status(db, thread, "error")
                elif event_type == "team_end" and event.get("status") == "cancelled":
                    # 团队任务被取消时不会发 finished, 恢复 idle
                    await _set_thread_status(db, thread, "idle")
                elif event_type == "title_update":
                    title_text = event.get("title", "")
                    if title_text and thread is not None and thread.title != title_text:
                        thread.title = title_text
                        db.add(thread)
                        await db.commit()

        except HarnessUnavailableError:
            await _set_thread_status(db, thread, "error")
            hub.publish(rs, json.dumps({'type': 'error', 'content': 'Harness 服务不可用，请稍后重试', 'status': 'service_unavailable'}))
        except Exception as e:
            logger.exception(f"SSE 泵任务异常: {e}")
            try:
                await _set_thread_status(db, thread, "error")
            except Exception:
                logger.exception("泵任务异常后更新线程状态失败")
            hub.publish(rs, json.dumps({'type': 'error', 'content': str(e), 'status': 'error'}))
        finally:
            hub.end_run(rs)


def _parse_thread_uuid(thread_id: str) -> uuid_mod.UUID:
    """畸形 UUID 返回 400 而非 500."""
    try:
        return uuid_mod.UUID(thread_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid thread_id")


@router.post("")
async def execute(
    req: ExecuteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    执行 Agent 任务 — 代理到 Harness 服务，流式返回 SSE 事件
    """
    harness = get_harness_client()
    thread_uuid = _parse_thread_uuid(req.thread_id)

    # 更新线程状态
    thread_result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    thread = thread_result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    thread.status = "running"
    db.add(thread)
    await db.commit()

    # ── 持久化用户消息 (HumanMessage 不会通过 SSE 事件发送) ──
    # 附件元数据随 extra_metadata.files 落库, 供历史消息还原附件文件卡片
    human_msg = Message(
        thread_id=thread_uuid,
        role="human",
        content=req.message,
        msg_type="text",
        extra_metadata={"files": req.files} if req.files else {},
        token_count=0,
    )
    db.add(human_msg)
    await db.commit()

    # @点名注入: InputBar 解析出的 mentions 转为给 Lead 的分派指令
    message = req.message
    if req.mentions:
        mention_str = ", ".join(f"@{m}" for m in req.mentions)
        message = f"[用户点名, 请优先安排以下成员参与: {mention_str}]\n{message}"

    # 启动后台泵任务 (事件生产/落库独立于本 HTTP 连接),
    # 本响应只是该 thread 事件流的一个订阅者
    hub = get_stream_hub()
    rs = hub.start_run(req.thread_id)
    rs.pump_task = asyncio.create_task(_pump_run(
        req.thread_id, thread_uuid, rs,
        lambda: harness.stream_execute(
            thread_id=req.thread_id,
            user_id=current_user.username,
            message=message,
            execution_graph=req.execution_graph,
            files=req.files,
            project_id=req.project_id,
            agent_name=req.agent_name,
            mode=req.mode,
        ),
    ))
    # 泵任务尚未被调度 (本函数内无 await), 此刻订阅必成功
    sub = hub.subscribe(req.thread_id, last_event_id=0)

    return StreamingResponse(
        _sse_frames(sub),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/{thread_id}/resume")
async def resume_execution(
    thread_id: str,
    last_event_id: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """断线续传 — 补发 seq > last_event_id 的缓冲事件后挂接实时流。

    绝不重发 execute 的 POST body (会重复执行); 本端点只做事件订阅。
    运行已结束 (not_running) 或缓冲覆盖不到 (gap) 时, 流内下发一次性
    resync 事件并结束, 由前端回退到状态轮询。
    """
    # 所有权校验
    thread_uuid = _parse_thread_uuid(thread_id)
    thread_result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    if thread_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    hub = get_stream_hub()
    sub = hub.subscribe(thread_id, max(last_event_id, 0))
    return StreamingResponse(
        _sse_frames(sub),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/{thread_id}/respond")
async def respond_to_clarification(
    thread_id: str,
    req: ClarificationResponse,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None, include_in_schema=False),
):
    """回复澄清请求 — 流式返回 Agent 恢复执行后的输出"""
    harness = get_harness_client()
    thread_uuid = _parse_thread_uuid(thread_id)

    # 更新线程状态 (含所有权校验)
    thread_result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    thread = thread_result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if thread.mode == "team" and not thread.project_id:
        # 数据异常: team 会话缺 project_id 无法走团队恢复路径, 明确报错而非静默降级
        raise HTTPException(status_code=400, detail="团队会话缺少 project_id，无法恢复")
    thread.status = "running"
    db.add(thread)
    await db.commit()

    # 持久化用户的澄清回答
    answer_msg = Message(
        thread_id=thread_uuid,
        role="human",
        content=req.answer,
        msg_type="text",
        extra_metadata={},
        token_count=0,
    )
    db.add(answer_msg)
    await db.commit()

    # ── s32: team 模式澄清恢复走 _execute_team() 路径 ──
    if thread.mode == "team" and thread.project_id:
        # user_id 必须是文件系统用户名 (非 UUID), 否则 Harness 找不到项目目录
        fs_uid = current_user.username or "default"

        def stream_factory() -> AsyncIterator[str]:
            return harness.stream_execute(
                thread_id=thread_id,
                user_id=fs_uid,
                message=req.answer,
                project_id=thread.project_id,
                agent_name=thread.agent_name,
                mode="team",
            )
    else:
        def stream_factory() -> AsyncIterator[str]:
            return harness.stream_respond_clarification(
                thread_id=thread_id,
                answer=req.answer,
            )

    # 与 execute 相同: 后台泵 + 订阅 (刷新后同样可经 resume 续流)
    hub = get_stream_hub()
    rs = hub.start_run(thread_id)
    rs.pump_task = asyncio.create_task(
        _pump_run(thread_id, thread_uuid, rs, stream_factory)
    )
    sub = hub.subscribe(thread_id, last_event_id=0)

    return StreamingResponse(
        _sse_frames(sub),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/{thread_id}/stop")
async def stop_execution(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """停止执行"""
    # 先校验归属, 再调 harness — 防止知道 thread_id 即可停止他人任务
    thread_uuid = _parse_thread_uuid(thread_id)
    thread_result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    thread = thread_result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    harness = get_harness_client()
    try:
        result = await harness.stop_execution(thread_id)

        thread.status = "idle"
        db.add(thread)
        await db.commit()

        return result
    except HarnessUnavailableError:
        raise HTTPException(status_code=503, detail="Harness 服务不可用")


@router.get("/{thread_id}/status")
async def get_execution_status(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询执行状态"""
    # 所有权校验
    thread_uuid = _parse_thread_uuid(thread_id)
    thread_result = await db.execute(
        select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
    )
    if thread_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    harness = get_harness_client()
    try:
        return await harness.get_status(thread_id)
    except HarnessUnavailableError:
        raise HTTPException(status_code=503, detail="Harness 服务不可用")


def _map_event_role(event_type: str, event: dict) -> str:
    """将 SSE 事件类型映射为消息角色"""
    mapping = {
        "message": "ai",
        "tool_call": "tool",
        "tool_result": "tool",
        "subagent_start": "subagent",
        "subagent_end": "subagent",
        "error": "system",
    }
    return mapping.get(event_type, "system")


def _build_team_message(thread_uuid, event_type: str, event: dict) -> Message:
    """构造 team_* 事件的持久化 Message (execute 与 respond 共用)"""
    if event_type == "team_message":
        _m = event.get("message", {}) or {}
        content = f"[{_m.get('from_agent', '')} → {_m.get('to_agent') or '全员'}] {_m.get('content', '')[:500]}"
    elif event_type == "team_task_update":
        _t = event.get("task", {}) or {}
        content = f"任务 [{_t.get('id', '')}] {_t.get('title', '')} → {_t.get('status', '')}"
    elif event_type == "member_status":
        content = f"成员 {event.get('agent_name', '')} → {event.get('status', '')} {event.get('task_title', '')}"
    elif event_type == "team_start":
        content = f"团队启动, 成员: {', '.join(event.get('members', []))}"
    elif event_type == "team_status":
        content = event.get("content", "") or event.get("phase", "")
    elif event_type == "message_injected":
        content = event.get("content", "")
    elif event_type == "team_degrade":
        content = f"Team 模式降级为单 Agent: {event.get('reason', '')}"
    else:
        content = f"团队结束 (status={event.get('status', '')}, rounds={event.get('total_rounds', '')})"
    return Message(
        thread_id=thread_uuid,
        role="system",
        content=content,
        msg_type=event_type,
        extra_metadata=event,
        token_count=0,
    )


# 持久化覆盖的 team/状态类事件 (含此前缺失导致刷新后消失的三种)
_PERSIST_TEAM_EVENTS = (
    "team_start", "team_task_update", "member_status", "team_message",
    "team_end", "team_status", "message_injected", "team_degrade",
)


class _StreamPersister:
    """流式 SSE 事件 → DB Message 的累积器 (execute 与 respond 共用).

    - 中间事件立即 db.add(), 由调用方在终态统一 commit
    - AI 流式文本 / thinking 按 tool_call 边界切段落库 — 与前端流式
      气泡形态一致 (前端遇 tool_call 即重置流式消息 ID 开新气泡),
      避免刷新/切换会话后多个气泡合并成一条长消息
    """

    def __init__(self, db: AsyncSession, thread_uuid) -> None:
        self._db = db
        self._thread_uuid = thread_uuid
        self._ai_segment = ""
        self._thinking_segment = ""
        self.token_total = 0

    def _flush_segments(self, token_count: int = 0) -> None:
        if self._ai_segment.strip():
            self._db.add(Message(
                thread_id=self._thread_uuid, role="ai",
                content=self._ai_segment, msg_type="message",
                extra_metadata={}, token_count=token_count,
            ))
        self._ai_segment = ""
        if self._thinking_segment.strip():
            self._db.add(Message(
                thread_id=self._thread_uuid, role="ai",
                content=self._thinking_segment, msg_type="thinking",
                extra_metadata={}, token_count=0,
            ))
        self._thinking_segment = ""

    def handle(self, event_type: str, event: dict) -> None:
        """累积/落库一个中间事件 (clarification 与终态事件由调用方处理)."""
        if event_type == "message" and event.get("content"):
            if event.get("msg_type"):
                # 完整消息事件 (静态兜底汇总等) — 独立成行, 不进流式段落
                self._db.add(Message(
                    thread_id=self._thread_uuid,
                    role=_map_event_role(event_type, event),
                    content=event.get("content", ""),
                    msg_type=event.get("msg_type", "message"),
                    extra_metadata=event,
                    token_count=event.get("tokens", {}).get("total_tokens", 0)
                    if isinstance(event.get("tokens"), dict) else 0,
                ))
            else:
                # 流式文本 chunk — 累积到当前段落
                self._ai_segment += event["content"]
        elif event_type == "thinking" and event.get("content"):
            self._thinking_segment += event["content"]
        elif event_type == "tool_call":
            # tool_call 是前端气泡边界 → 先把已累积的段落落库
            self._flush_segments()
            self._db.add(Message(
                thread_id=self._thread_uuid,
                role=_map_event_role(event_type, event),
                content=event.get("content", "") or event.get("tool_result", "")
                or event.get("instruction", ""),
                msg_type=event_type, extra_metadata=event, token_count=0,
            ))
        elif event_type in ("tool_result", "subagent_start", "subagent_end", "error"):
            self._db.add(Message(
                thread_id=self._thread_uuid,
                role=_map_event_role(event_type, event),
                content=event.get("content", "") or event.get("tool_result", "")
                or event.get("instruction", ""),
                msg_type=event_type, extra_metadata=event, token_count=0,
            ))
        elif event_type in _PERSIST_TEAM_EVENTS:
            self._db.add(_build_team_message(self._thread_uuid, event_type, event))
        elif event_type == "token_usage":
            tokens = event.get("tokens", {})
            self.token_total = tokens.get("total_tokens", 0) if isinstance(tokens, dict) else 0

    def finalize(self) -> None:
        """终态 (finished) — 冲刷最后一个段落并回填 token 数."""
        self._flush_segments(token_count=self.token_total)


async def _persist_clarification(db: AsyncSession, thread: Thread, thread_uuid, event: dict) -> None:
    """澄清暂停落库: 持久化澄清请求 + 提交本轮累积事件 + 标记 suspended。

    澄清时后端正常结束 SSE 流但不发 finished, 若不立即 commit,
    session 关闭会回滚本轮所有未提交事件; 前端刷新后靠这条
    msg_type="clarification" 的消息恢复待回答状态。
    """
    req = event.get("request", {}) or {}
    msg = Message(
        thread_id=thread_uuid,
        role="system",
        content=req.get("question", ""),
        msg_type="clarification",
        extra_metadata=event,
        token_count=0,
    )
    db.add(msg)
    if thread is not None:
        thread.status = "suspended"
        db.add(thread)
    await db.commit()
