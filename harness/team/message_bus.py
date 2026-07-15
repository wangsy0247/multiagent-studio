"""TeamMessageBus — Agent 间消息总线 (per-agent inbox + drain-on-read).

参考 learn-claude-code  MessageBus 设计:
- 每个 agent 独立的 inbox JSONL 文件
- send: 追加写入收件人 inbox
- read_inbox: 读完即清空 (drain-on-read), 无需游标追踪
- 实时通知: asyncio.Event 驱动唤醒
- 消息循环检测: 检测 A↔B 乒乓消息

目录结构:
    {data_root}/users/{user_id}/projects/{project_id}/messages/inbox/
        lead.jsonl
        alice.jsonl
        bob.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from harness.config.paths import get_paths
from harness.team.models import TeamMessage, TeamMessageType

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamMessageBus:
    """Agent 间消息总线 — per-agent inbox + drain-on-read.

    每个 agent 拥有独立 inbox 文件, 用 drain-on-read 替代游标追踪,
    比单文件 + 游标方案更简洁、更不易出错。
    """

    def __init__(self, project_id: str, user_id: str = "default") -> None:
        self._project_id = project_id
        self._user_id = user_id
        paths = get_paths()
        self._inbox_dir = paths.base_dir / "users" / user_id / "projects" / project_id / "messages" / "inbox"
        self._inbox_dir.mkdir(parents=True, exist_ok=True)

        # ── 实时通知: agent_name → asyncio.Event ──
        self._events: dict[str, asyncio.Event] = {}

        # ── 已知 agent 列表 (用于广播) ──
        self._known_agents: set[str] = set()

    # ------------------------------------------------------------------
    # 内部: inbox 文件路径
    # ------------------------------------------------------------------

    def _inbox_path(self, agent_name: str) -> Path:
        return self._inbox_dir / f"{agent_name}.jsonl"

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def send(self, message: TeamMessage) -> None:
        """发送消息 — 写入收件人 inbox, 触发实时通知.

        to_agent=None 表示广播给所有已知 agent (除发送者外).
        """
        if not message.created_at:
            message.created_at = _now_iso()
        if not message.id:
            message.id = str(uuid.uuid4())[:8]

        line = message.model_dump_json() + "\n"

        if message.to_agent:
            # ── 点对点 ──
            self._append(message.to_agent, line)
            self._notify(message.to_agent)
        else:
            # ── 广播 ──
            for agent in list(self._known_agents):
                if agent != message.from_agent:
                    self._append(agent, line)
                    self._notify(agent)

        logger.debug(
            "Message sent: from=%s to=%s type=%s",
            message.from_agent,
            message.to_agent or "broadcast",
            message.msg_type,
        )

    async def read_inbox(self, agent_name: str) -> list[TeamMessage]:
        """读取 agent 的收件箱并清空 (drain-on-read).

        无需游标追踪 — 读完即清, 天然无竞态.
        """
        path = self._inbox_path(agent_name)
        if not path.exists():
            return []

        messages: list[TeamMessage] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = TeamMessage.model_validate_json(line)
                        messages.append(msg)
                    except Exception:
                        logger.warning("Failed to parse message from %s inbox", agent_name)
        except Exception as exc:
            logger.error("Failed to read inbox for '%s': %s", agent_name, exc)
            return []

        # 清空 inbox
        try:
            path.write_text("", encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to clear inbox for '%s': %s", agent_name, exc)

        return messages

    # ------------------------------------------------------------------
    # 实时通知
    # ------------------------------------------------------------------

    def _notify(self, agent_name: str) -> None:
        """触发 agent 的通知事件."""
        event = self._events.get(agent_name)
        if event:
            event.set()

    def get_event(self, agent_name: str) -> asyncio.Event:
        """获取 agent 的通知事件 (惰性创建)."""
        if agent_name not in self._events:
            self._events[agent_name] = asyncio.Event()
        return self._events[agent_name]

    async def wait_for_message(
        self, agent_name: str, timeout: float = 30.0,
    ) -> list[TeamMessage]:
        """事件驱动等待新消息 — 替代 sleep() 轮询.

        有新消息时立即返回, 超时返回空列表.
        """
        event = self.get_event(agent_name)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return []

        return await self.read_inbox(agent_name)

    # ------------------------------------------------------------------
    # Agent 注册 (用于广播)
    # ------------------------------------------------------------------

    def register_agent(self, agent_name: str) -> None:
        """注册 agent — 使其能接收到广播消息."""
        self._known_agents.add(agent_name)
        # 惰性创建 event
        self.get_event(agent_name)

    def unregister_agent(self, agent_name: str) -> None:
        """注销 agent."""
        self._known_agents.discard(agent_name)
        self._events.pop(agent_name, None)

    # ------------------------------------------------------------------
    # 消息循环检测
    # ------------------------------------------------------------------

    async def check_message_loop(
        self, agent_a: str, agent_b: str, window: int = 10,
    ) -> bool:
        """检测两个 Agent 之间是否存在消息循环 (A→B→A→B...).

        通过扫描两个 agent 的 inbox 文件检测乒乓模式.
        """
        msgs_a = await self._read_all(agent_a)
        msgs_b = await self._read_all(agent_b)
        all_msgs = sorted(msgs_a + msgs_b, key=lambda m: m.created_at)

        ab_msgs = [
            m for m in all_msgs
            if {m.from_agent, m.to_agent} == {agent_a, agent_b}
        ]
        if len(ab_msgs) < 4:
            return False

        recent = ab_msgs[-4:]
        senders = [m.from_agent for m in recent]
        return senders == [agent_a, agent_b, agent_a, agent_b]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _append(self, agent_name: str, line: str) -> None:
        """追加一行到 agent 的 inbox 文件."""
        path = self._inbox_path(agent_name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    async def _read_all(self, agent_name: str) -> list[TeamMessage]:
        """读取 agent 的全部消息 (不清空, 用于循环检测)."""
        path = self._inbox_path(agent_name)
        if not path.exists():
            return []
        messages: list[TeamMessage] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(TeamMessage.model_validate_json(line))
                except Exception:
                    pass
        return messages
