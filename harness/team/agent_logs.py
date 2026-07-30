"""Agent 对话日志写入器 — 将 agent 的 LLM 对话历史写入 JSONL 文件.

供 TeammateAgent 在任务执行过程中调用, 将每条消息和任务边界写入
per-agent 的 JSONL 文件。App API 直接读取这些文件, 前端按 agent 隔离展示。

存储路径: {base_dir}/users/{uid}/projects/{pid}/threads/{tid}/agent_logs/{agent_name}.jsonl

格式:
  {"type":"message","role":"human","content":"任务: ...","task_id":"t1","tool_name":null,"timestamp":"..."}
  {"type":"message","role":"ai","content":"我来实现...","task_id":"t1","tool_name":null,"timestamp":"..."}
  {"type":"message","role":"tool","content":"(result)","task_id":"t1","tool_name":"read_file","timestamp":"..."}
  {"type":"task_boundary","task_id":"t1","title":"实现登录","status":"completed","summary":"...","timestamp":"..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class AgentLogWriter:
    """将 agent 对话写入 JSONL 文件, 一行一条记录."""

    def __init__(
        self,
        base_dir: Path,
        project_id: str,
        thread_id: str,
        agent_name: str,
        user_id: str = "default",
    ) -> None:
        self._agent_name = agent_name
        self._file_path = (
            base_dir
            / "users"
            / user_id
            / "projects"
            / project_id
            / "threads"
            / thread_id
            / "agent_logs"
            / f"{agent_name}.jsonl"
        )
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "AgentLogWriter created for '%s' at %s", agent_name, self._file_path,
        )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def write_message(
        self,
        role: str,
        content: str,
        task_id: str,
        tool_name: str | None = None,
    ) -> None:
        """写入一条对话消息.

        Args:
            role: ``"human"`` | ``"ai"`` | ``"tool"``
            content: 消息文本 (tool 时截断到 1000 字符)
            task_id: 所属任务 ID
            tool_name: tool 角色时的工具名
        """
        if role == "tool":
            content = content[:1000]
        entry: dict[str, object] = {
            "type": "message",
            "role": role,
            "content": content,
            "task_id": task_id,
            "tool_name": tool_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(entry)

    def write_task_boundary(
        self,
        task_id: str,
        title: str,
        status: str,
        summary: str = "",
    ) -> None:
        """写入任务边界标记 — 前端用于渲染任务分隔条.

        Args:
            task_id: 任务 ID
            title: 任务标题
            status: 终态状态值 (completed / failed / approved)
            summary: 执行结果摘要 (截断到 300 字符)
        """
        entry: dict[str, object] = {
            "type": "task_boundary",
            "task_id": task_id,
            "title": title,
            "status": status,
            "summary": summary[:300] if summary else "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._append(entry)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _append(self, entry: dict[str, object]) -> None:
        """追加一行 JSON 到日志文件 (无锁 — 单 agent 单线程写入)."""
        try:
            with open(self._file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception(
                "AgentLogWriter: failed to write entry for '%s'", self._agent_name,
            )
