"""TeamTracer — Agent Team 全链路追踪器 (本地文件 + Langfuse 双通道).

追踪数据同时写入两个通道:
  1. 本地 JSONL 文件 — 始终启用 (无需外部服务)
  2. Langfuse 服务端 — 配置 LANGFUSE_PUBLIC_KEY 后启用

文件结构:
    {trace_dir}/
        trace.jsonl     ← 完整事件时间线 (每行一个 JSON)
        summary.json    ← 最终汇总

使用方式:
    tracer = TeamTracer(trace_dir=Path(...), session_id=thread_id, user_id=user_id)
    tracer.trace_phase("planning")
    tracer.trace_task_event(task_id, "created", metadata={...})
    tracer.flush()      # 确保数据落盘
    tracer.shutdown()   # 关闭文件
"""

from __future__ import annotations

import json as _json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 尝试导入 langfuse ──
try:
    from langfuse import Langfuse as _Langfuse
    from langfuse.langchain import CallbackHandler as _LangfuseCallbackHandler
    _HAS_LANGFUSE = True
except ImportError:
    _Langfuse = None  # type: ignore[assignment]
    _LangfuseCallbackHandler = None  # type: ignore[assignment]
    _HAS_LANGFUSE = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamTracer:
    """Agent Team 专用追踪器 — 本地 JSONL + Langfuse 双通道.

    本地文件始终写入 (如果指定了 trace_dir); Langfuse 通道需要 API key.
    """

    def __init__(
        self,
        *,
        trace_dir: Path | str | None = None,
        session_id: str = "",
        user_id: str = "",
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._seq: int = 0
        self._project_id: str = ""
        self._thread_id: str = ""

        # ── 文件通道 ──
        self._trace_file: Any = None
        self._trace_dir: Path | None = None
        if trace_dir:
            self._trace_dir = Path(trace_dir)
            self._trace_dir.mkdir(parents=True, exist_ok=True)
            self._trace_file = open(self._trace_dir / "trace.jsonl", "a", encoding="utf-8")
            logger.info("TeamTracer: file tracing enabled → %s/trace.jsonl", self._trace_dir)

        # ── Langfuse 通道 ──
        self._langfuse: Any = None
        self._langfuse_enabled: bool = False
        self._root_obs: Any = None  # 根 observation, end on trace_team_end
        if _HAS_LANGFUSE and (public_key or secret_key):
            try:
                kwargs: dict[str, str] = {}
                if public_key:
                    kwargs["public_key"] = public_key
                if secret_key:
                    kwargs["secret_key"] = secret_key
                if host:
                    kwargs["host"] = host
                self._langfuse = _Langfuse(**kwargs)
                self._langfuse_enabled = True
                logger.info("TeamTracer: Langfuse enabled (host=%s)", host or "cloud")
            except Exception as exc:
                logger.warning("TeamTracer: Langfuse init failed: %s", exc)

        if not self._trace_file and not self._langfuse_enabled:
            logger.info("TeamTracer: no channels available — tracing disabled")

    # ------------------------------------------------------------------
    # 内部: 写入文件
    # ------------------------------------------------------------------

    def _write_event(self, event_type: str, **data: Any) -> None:
        """写入一条事件到本地 JSONL 文件."""
        if self._trace_file is None:
            return
        self._seq += 1
        record = {
            "timestamp": _now_iso(),
            "seq": self._seq,
            "type": event_type,
        }
        record.update(data)
        try:
            self._trace_file.write(_json.dumps(record, ensure_ascii=False) + "\n")
            self._trace_file.flush()
        except Exception as exc:
            logger.warning("TeamTracer: file write failed: %s", exc)

    # ------------------------------------------------------------------
    # 公共追踪方法 (双通道)
    # ------------------------------------------------------------------

    def trace_team_start(
        self,
        message: str,
        *,
        project_id: str = "",
        thread_id: str = "",
        members: list[str] | None = None,
    ) -> None:
        """记录 Team 执行开始."""
        self._project_id = project_id
        self._thread_id = thread_id
        self._write_event("team_start", message=message[:500], project_id=project_id,
                          thread_id=thread_id, members=members or [],
                          user_id=self._user_id)
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                self._root_obs = self._langfuse.start_observation(
                    name="Team Execution",
                    as_type="span",
                    input=message[:500],
                    metadata={"project_id": project_id, "thread_id": thread_id,
                              "user_id": self._user_id, "type": "team_execution"},
                )
                logger.info("TeamTracer: Langfuse trace started — %s",
                            getattr(self._root_obs, 'trace_id', '?'))
            except Exception as exc:
                logger.warning("Langfuse trace_team_start failed: %s", exc)

    def trace_team_end(self, status: str, total_rounds: int = 0) -> None:
        """记录 Team 执行结束."""
        self._write_event("team_end", status=status, total_rounds=total_rounds)
        # 关闭 Langfuse root observation
        if self._root_obs is not None:
            try:
                self._root_obs.update(output={"status": status, "total_rounds": total_rounds})
                self._root_obs.end()
                self._root_obs = None
            except Exception as exc:
                logger.warning("TeamTracer: root obs end failed: %s", exc)
        # 写入汇总文件
        if self._trace_dir:
            try:
                summary = {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "project_id": self._project_id,
                    "thread_id": self._thread_id,
                    "status": status,
                    "total_rounds": total_rounds,
                    "total_events": self._seq,
                    "finished_at": _now_iso(),
                }
                with open(self._trace_dir / "summary.json", "w", encoding="utf-8") as f:
                    _json.dump(summary, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                logger.warning("TeamTracer: summary write failed: %s", exc)

    def trace_phase(self, phase: str) -> None:
        """记录阶段转换.

        Args:
            phase: "planning" | "dispatching" | "synthesizing"
        """
        self._write_event("phase", phase=phase)

    def trace_task_event(
        self,
        task_id: str,
        event_type: str,
        *,
        title: str = "",
        assigned_agent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录任务生命周期事件.

        event_type: "created" | "claimed" | "assigned" | "in_progress" |
                     "completed" | "failed" | "cancelled"
        """
        self._write_event("task_event", task_id=task_id, event=event_type,
                          title=title, assigned_agent=assigned_agent,
                          metadata=metadata or {})
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                emoji = {"created": "📝", "claimed": "🙋", "assigned": "📋",
                         "completed": "✅", "failed": "❌", "cancelled": "🚫",
                         "in_progress": "🔄"}
                self._langfuse.create_event(
                    name=f"{emoji.get(event_type, '•')} Task {event_type}: {title or task_id}",
                    metadata={"task_id": task_id, "event_type": event_type,
                              "agent": assigned_agent, **(metadata or {})},
                    input=task_id,
                )
            except Exception:
                pass

    def trace_teammate_work_start(
        self,
        agent_name: str,
        task_id: str | None = None,
        *,
        role: str = "member",
    ) -> None:
        """记录 Teammate 开始工作."""
        mode = "monitoring" if task_id is None else f"task:{task_id}"
        self._write_event("teammate_work_start", agent_name=agent_name,
                          task_id=task_id, role=role, mode=mode)

    def trace_teammate_work_end(
        self,
        agent_name: str,
        task_id: str | None = None,
        *,
        role: str = "member",
        status: str = "completed",
    ) -> None:
        """记录 Teammate 工作结束."""
        self._write_event("teammate_work_end", agent_name=agent_name,
                          task_id=task_id, role=role, status=status)

    def trace_message(
        self,
        from_agent: str,
        to_agent: str | None,
        msg_type: str,
        content: str,
        *,
        task_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """记录 Agent 间消息通信."""
        self._write_event("message", from_agent=from_agent,
                          to_agent=to_agent or "broadcast",
                          msg_type=msg_type, content=content[:300],
                          task_id=task_id, request_id=request_id)
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                icon = {"text": "💬", "broadcast": "📢", "lifecycle": "🔄",
                        "shutdown_request": "🔌", "shutdown_response": "🔌",
                        "plan_approval_request": "📋", "plan_approval_response": "📋"}
                to_label = to_agent or "all"
                self._langfuse.create_event(
                    name=f"{icon.get(msg_type, '📨')} {from_agent} → {to_label}: {msg_type}",
                    metadata={"from_agent": from_agent, "to_agent": to_agent,
                              "msg_type": msg_type, "task_id": task_id, "request_id": request_id},
                    input=content[:200],
                )
            except Exception:
                pass

    def trace_error(self, error_message: str, *, metadata: dict[str, Any] | None = None) -> None:
        """记录错误事件."""
        self._write_event("error", error=error_message, metadata=metadata or {})
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                self._langfuse.create_event(
                    name=f"❌ Error: {error_message[:100]}",
                    metadata={"error": error_message, **(metadata or {})},
                )
            except Exception:
                pass

    def trace_llm_call(self, agent_name: str, model: str, tokens: int = 0,
                       duration_ms: int = 0) -> None:
        """记录 LLM 调用统计 (聚合数据)."""
        self._write_event("llm_call", agent_name=agent_name, model=model,
                          tokens=tokens, duration_ms=duration_ms)

    def trace_tool_call(self, agent_name: str, tool_name: str,
                        tool_input: str = "", tool_output: str = "",
                        duration_ms: int = 0) -> None:
        """记录工具调用."""
        self._write_event("tool_call", agent_name=agent_name, tool_name=tool_name,
                          input_preview=tool_input[:200],
                          output_preview=tool_output[:200],
                          duration_ms=duration_ms)

    # ------------------------------------------------------------------
    # LangChain 集成 (仅 Langfuse)
    # ------------------------------------------------------------------

    def get_langchain_callback(self):
        """获取 Langfuse CallbackHandler — 自动追踪 LLM + Tool 调用."""
        if not self._langfuse_enabled or _LangfuseCallbackHandler is None:
            return None
        try:
            return _LangfuseCallbackHandler()
        except Exception as exc:
            logger.warning("TeamTracer: CallbackHandler failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """刷新所有通道的缓冲数据."""
        if self._trace_file is not None:
            try:
                self._trace_file.flush()
            except Exception:
                pass
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                self._langfuse.flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        """关闭 tracer."""
        if self._trace_file is not None:
            try:
                self._trace_file.close()
            except Exception:
                pass
            self._trace_file = None
        if self._langfuse_enabled and self._langfuse is not None:
            try:
                self._langfuse.shutdown()
            except Exception:
                pass

    @property
    def is_enabled(self) -> bool:
        """任一通道可用即为 enabled."""
        return self._trace_file is not None or self._langfuse_enabled

    @property
    def trace_dir(self) -> Path | None:
        return self._trace_dir
