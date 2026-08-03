"""
执行 API 路由: 代理 Harness 的 SSE 执行、停止、状态查询
"""

import json
import logging
import uuid as uuid_mod
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update

from app.db.engine import get_db
from app.api.deps import get_current_user, resolve_fs_user_id
from app.models.user import User
from app.models.thread import Thread
from app.models.message import Message
from app.services.harness_client import get_harness_client, HarnessUnavailableError

logger = logging.getLogger(__name__)
router = APIRouter()


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


class ClarificationResponse(BaseModel):
    answer: str


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
    thread_uuid = uuid_mod.UUID(req.thread_id)

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

    async def event_generator():
        """SSE 事件流生成器 — 转发 Harness SSE 事件并持久化关键事件"""
        # ── 持久化用户消息 (HumanMessage 不会通过 SSE 事件发送) ──
        human_msg = Message(
            thread_id=thread_uuid,
            role="human",
            content=req.message,
            msg_type="text",
            extra_metadata={},
            token_count=0,
        )
        db.add(human_msg)
        await db.commit()

        # 流式事件持久化器 — 中间事件即时 db.add, 终态统一 commit;
        # AI 文本/thinking 按 tool_call 边界切段 (与前端气泡形态一致)
        persister = _StreamPersister(db, thread_uuid)
        try:
            async for event_json in harness.stream_execute(
                thread_id=req.thread_id,
                user_id=current_user.username,
                message=req.message,
                execution_graph=req.execution_graph,
                files=req.files,
                project_id=req.project_id,
                agent_name=req.agent_name,
                mode=req.mode,
            ):
                try:
                    event = json.loads(event_json) if isinstance(event_json, str) else event_json
                except json.JSONDecodeError:
                    yield f"data: {event_json}\n\n"
                    continue

                event_type = event.get("type", "")

                if event_type == "clarification":
                    # 澄清暂停: 后端会正常结束流但不发 finished, 必须立即落库,
                    # 否则 session 关闭回滚导致本轮事件全部丢失
                    await _persist_clarification(db, thread, thread_uuid, event)
                else:
                    persister.handle(event_type, event)

                # 转发 SSE 到前端
                yield f"data: {json.dumps(event, default=str)}\n\n"

                # ── 终态事件: 一次性 commit 所有累积数据 ──
                if event_type == "finished":
                    persister.finalize()
                    thread.status = "finished"
                    db.add(thread)
                    await db.commit()
                elif event_type == "error":
                    thread.status = "error"
                    db.add(thread)
                    await db.commit()
                elif event_type == "team_error":
                    # team 失败走 team_error 而非 error, 同样要落库终态,
                    # 否则会话列表永远显示 running
                    thread.status = "error"
                    db.add(thread)
                    await db.commit()
                elif event_type == "team_end" and event.get("status") == "cancelled":
                    # 团队任务被取消时不会发 finished, 恢复 idle
                    thread.status = "idle"
                    db.add(thread)
                    await db.commit()
                elif event_type == "title_update":
                    title_text = event.get("title", "")
                    if title_text and thread.title != title_text:
                        thread.title = title_text
                        db.add(thread)
                        await db.commit()

        except HarnessUnavailableError:
            thread.status = "error"
            db.add(thread)
            await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'content': 'Harness 服务不可用，请稍后重试', 'status': 'service_unavailable'})}\n\n"
        except Exception as e:
            logger.exception(f"SSE 流异常: {e}")
            thread.status = "error"
            db.add(thread)
            await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'content': str(e), 'status': 'error'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
    thread_uuid = uuid_mod.UUID(thread_id)

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

    async def event_generator():
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

        persister = _StreamPersister(db, thread_uuid)
        try:
            # ── s32: team 模式澄清恢复走 _execute_team() 路径 ──
            if thread.mode == "team" and thread.project_id:
                # user_id 必须是文件系统用户名 (非 UUID), 否则 Harness 找不到项目目录
                fs_uid = current_user.username or "default"
                event_stream = harness.stream_execute(
                    thread_id=thread_id,
                    user_id=fs_uid,
                    message=req.answer,
                    project_id=thread.project_id,
                    agent_name=thread.agent_name,
                    mode="team",
                )
            else:
                event_stream = harness.stream_respond_clarification(
                    thread_id=thread_id,
                    answer=req.answer,
                )
            async for event_json in event_stream:
                try:
                    event = json.loads(event_json) if isinstance(event_json, str) else event_json
                except json.JSONDecodeError:
                    yield f"data: {event_json}\n\n"
                    continue

                event_type = event.get("type", "")

                if event_type == "clarification":
                    # 恢复执行中再次澄清: 同样立即落库, 避免流结束无 finished 时回滚
                    await _persist_clarification(db, thread, thread_uuid, event)
                else:
                    persister.handle(event_type, event)

                yield f"data: {json.dumps(event, default=str)}\n\n"

                if event_type == "finished":
                    persister.finalize()
                    thread.status = "finished"
                    db.add(thread)
                    await db.commit()
                elif event_type == "error":
                    thread.status = "error"
                    db.add(thread)
                    await db.commit()
                elif event_type == "team_error":
                    # team 失败走 team_error 而非 error, 同样要落库终态,
                    # 否则会话列表永远显示 running
                    thread.status = "error"
                    db.add(thread)
                    await db.commit()
                elif event_type == "team_end" and event.get("status") == "cancelled":
                    # 团队任务被取消时不会发 finished, 恢复 idle
                    thread.status = "idle"
                    db.add(thread)
                    await db.commit()

        except HarnessUnavailableError:
            thread.status = "error"
            db.add(thread)
            await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'content': 'Harness 服务不可用', 'status': 'service_unavailable'})}\n\n"
        except Exception as e:
            logger.exception(f"Clarification respond SSE 异常: {e}")
            thread.status = "error"
            db.add(thread)
            await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'content': str(e), 'status': 'error'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{thread_id}/stop")
async def stop_execution(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """停止执行"""
    harness = get_harness_client()
    thread_uuid = uuid_mod.UUID(thread_id)
    try:
        result = await harness.stop_execution(thread_id)

        # 更新线程状态 (含所有权校验)
        thread_result = await db.execute(
            select(Thread).where(Thread.id == thread_uuid, Thread.user_id == current_user.id)
        )
        thread = thread_result.scalar_one_or_none()
        if thread:
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
    thread_uuid = uuid_mod.UUID(thread_id)
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
    """流式 SSE 事件 → DB Message 的累积器 (execute 与 respond generator 共用).

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
    thread.status = "suspended"
    db.add(thread)
    await db.commit()
