"""
执行 API 路由: 代理 Harness 的 SSE 执行、停止、状态查询
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update

from app.db.engine import get_db
from app.api.deps import get_current_user
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

    # 更新线程状态
    thread_result = await db.execute(
        select(Thread).where(Thread.id == req.thread_id, Thread.user_id == current_user.id)
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
            thread_id=req.thread_id,
            role="human",
            content=req.message,
            msg_type="text",
            extra_metadata={},
            token_count=0,
        )
        db.add(human_msg)
        await db.commit()

        # 追踪最后一条 AI 消息的 ID，用于 token_usage 回填
        _last_ai_message_id: str | None = None
        # 累积 token 流式输出的 AI 回复文本（在 finished 时持久化）
        _accumulated_ai_text: str = ""
        try:
            async for event_json in harness.stream_execute(
                thread_id=req.thread_id,
                user_id=current_user.username,  # 文件系统目录统一使用 username
                message=req.message,
                execution_graph=req.execution_graph,
                files=req.files,
                project_id=req.project_id,
                agent_name=req.agent_name,
                mode=req.mode,
            ):
                # 解析事件
                try:
                    event = json.loads(event_json) if isinstance(event_json, str) else event_json
                except json.JSONDecodeError:
                    yield f"data: {event_json}\n\n"
                    continue

                event_type = event.get("type", "")

                # ── 累积 token 流式输出的 AI 回复文本 ──
                if event_type == "message" and event.get("content"):
                    _accumulated_ai_text += event["content"]

                # ── 持久化关键事件 ──
                if event_type in ("tool_call", "tool_result", "subagent_start", "subagent_end", "error"):
                    msg = Message(
                        thread_id=req.thread_id,
                        role=_map_event_role(event_type, event),
                        content=event.get("content", "") or event.get("tool_result", "") or event.get("instruction", ""),
                        msg_type=event_type,
                        extra_metadata=event,
                        token_count=0,
                    )
                    db.add(msg)
                    await db.commit()
                    if event_type == "tool_call":
                        _last_ai_message_id = str(msg.id)
                elif event_type == "message" and event.get("msg_type"):
                    # Complete (non-streaming) message — persist once
                    msg = Message(
                        thread_id=req.thread_id,
                        role=_map_event_role(event_type, event),
                        content=event.get("content", ""),
                        msg_type=event.get("msg_type", "message"),
                        extra_metadata=event,
                        token_count=event.get("tokens", {}).get("total_tokens", 0) if isinstance(event.get("tokens"), dict) else 0,
                    )
                    db.add(msg)
                    await db.commit()
                    _last_ai_message_id = str(msg.id)
                elif event_type == "token_usage" and _last_ai_message_id:
                    # 回填已持久化消息的 token 统计
                    try:
                        tokens = event.get("tokens", {})
                        total = tokens.get("total_tokens", 0) if isinstance(tokens, dict) else 0
                        stmt = (
                            sa_update(Message)
                            .where(Message.id == _last_ai_message_id)
                            .values(token_count=total)
                        )
                        await db.execute(stmt)
                        await db.commit()
                    except Exception:
                        pass  # best-effort; don't break the SSE stream

                # 转发 SSE 到前端
                yield f"data: {json.dumps(event, default=str)}\n\n"

                # 处理线程结束
                if event_type == "finished":
                    # ── 持久化累积的 AI 回复文本 ──
                    if _accumulated_ai_text.strip():
                        msg = Message(
                            thread_id=req.thread_id,
                            role="ai",
                            content=_accumulated_ai_text,
                            msg_type="message",
                            extra_metadata={},
                            token_count=0,
                        )
                        db.add(msg)
                        await db.commit()
                        _last_ai_message_id = str(msg.id)
                    thread.status = "finished"
                    db.add(thread)
                    await db.commit()
                elif event_type == "error":
                    thread.status = "error"
                    db.add(thread)
                    await db.commit()
                elif event_type == "title_update":
                    # ── 持久化自动生成的标题 ──
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
):
    """回复澄清请求 — 流式返回 Agent 恢复执行后的输出"""
    harness = get_harness_client()

    # 更新线程状态
    thread_result = await db.execute(select(Thread).where(Thread.id == thread_id))
    thread = thread_result.scalar_one_or_none()
    if thread:
        thread.status = "running"
        db.add(thread)
        await db.commit()

    async def event_generator():
        try:
            async for event_json in harness.stream_respond_clarification(
                thread_id=thread_id,
                answer=req.answer,
            ):
                try:
                    event = json.loads(event_json) if isinstance(event_json, str) else event_json
                except json.JSONDecodeError:
                    yield f"data: {event_json}\n\n"
                    continue

                yield f"data: {json.dumps(event, default=str)}\n\n"

                event_type = event.get("type", "")
                if event_type == "finished":
                    if thread:
                        thread.status = "finished"
                        db.add(thread)
                        await db.commit()
                elif event_type == "error":
                    if thread:
                        thread.status = "error"
                        db.add(thread)
                        await db.commit()

        except HarnessUnavailableError:
            if thread:
                thread.status = "error"
                db.add(thread)
                await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'content': 'Harness 服务不可用', 'status': 'service_unavailable'})}\n\n"
        except Exception as e:
            logger.exception(f"Clarification respond SSE 异常: {e}")
            if thread:
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
    try:
        result = await harness.stop_execution(thread_id)

        # 更新线程状态
        thread_result = await db.execute(select(Thread).where(Thread.id == thread_id))
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
):
    """查询执行状态"""
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
