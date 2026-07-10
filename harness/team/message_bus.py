"""TeamMessageBus — Agent 间消息总线。

消息以 JSONL 格式持久化:
    {data_root}/users/{user_id}/team_messages/{project_id}/mailbox.jsonl

支持:
- 点对点消息和广播
- 未读消息追踪（基于游标）
- 实时通知（asyncio.Event）
- 消息循环检测
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config.paths import get_paths
from harness.team.models import TeamMessage, TeamMessageType

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamMessageBus:
    """Agent 间消息总线 — JSONL 持久化 + 内存事件通知。

    每个 project 一个 mailbox.jsonl 文件，追加写入，无需文件锁。
    """

    def __init__(self, project_id: str, user_id: str = "default") -> None:
        self._project_id = project_id
        self._user_id = user_id
        paths = get_paths()
        self._msgs_dir = paths.base_dir / "users" / user_id / "team_messages" / project_id
        self._msgs_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._msgs_dir / "mailbox.jsonl"

        # ── 游标: agent_name -> 已读消息 ID 集合 ──
        self._read_cursors: dict[str, set[str]] = {}
        self._cursor_file = self._msgs_dir / ".read_cursors.json"
        self._load_cursors()

        # ── 实时通知 ──
        self._events: dict[str, asyncio.Event] = {}  # agent_name -> Event

    # ------------------------------------------------------------------
    # 游标持久化
    # ------------------------------------------------------------------

    def _load_cursors(self) -> None:
        if self._cursor_file.exists():
            try:
                with open(self._cursor_file, encoding="utf-8") as f:
                    data = json.load(f)
                self._read_cursors = {
                    k: set(v) for k, v in data.items()
                }
            except Exception:
                self._read_cursors = {}

    def _save_cursors(self) -> None:
        try:
            data = {k: list(v) for k, v in self._read_cursors.items()}
            with open(self._cursor_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as exc:
            logger.warning("Failed to save read cursors: %s", exc)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def send(self, message: TeamMessage) -> None:
        """发送消息 — 追加到 JSONL 文件并触发实时通知."""
        if not message.created_at:
            message.created_at = _now_iso()
        if not message.id:
            message.id = str(uuid.uuid4())[:8]

        # 持久化（追加写入，JSONL 天然并发安全）
        line = message.model_dump_json() + "\n"
        with open(self._file, "a", encoding="utf-8") as f:
            f.write(line)

        # 触发实时通知
        if message.to_agent:
            event = self._events.get(message.to_agent)
            if event:
                event.set()
        else:
            # 广播：通知所有人
            for name, event in self._events.items():
                if name != message.from_agent:
                    event.set()

        logger.debug(
            "Message sent: from=%s to=%s type=%s",
            message.from_agent,
            message.to_agent or "broadcast",
            message.msg_type,
        )

    async def get_messages(
        self,
        agent_name: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[TeamMessage]:
        """读取消息，可按收件人和时间过滤."""
        if not self._file.exists():
            return []

        messages: list[TeamMessage] = []
        with open(self._file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = TeamMessage.model_validate_json(line)
                except Exception:
                    continue

                # 过滤
                if agent_name is not None:
                    if msg.to_agent is not None and msg.to_agent != agent_name:
                        continue  # 点对点但不是发给此 agent
                    if msg.to_agent is None and msg.from_agent == agent_name:
                        continue  # 广播但来自自己

                if since is not None and msg.created_at <= since:
                    continue

                messages.append(msg)

        # 按时间排序，限制数量
        messages.sort(key=lambda m: m.created_at)
        return messages[-limit:]

    async def get_unread(self, agent_name: str) -> list[TeamMessage]:
        """返回发给 agent_name 的未读消息."""
        all_msgs = await self.get_messages(agent_name=agent_name)
        read_ids = self._read_cursors.get(agent_name, set())
        return [m for m in all_msgs if m.id not in read_ids]

    async def mark_read(self, agent_name: str, message_ids: list[str]) -> None:
        """标记消息为已读."""
        if agent_name not in self._read_cursors:
            self._read_cursors[agent_name] = set()
        self._read_cursors[agent_name].update(message_ids)
        self._save_cursors()

    async def mark_all_read(self, agent_name: str) -> None:
        """标记该 agent 的所有消息为已读."""
        all_msgs = await self.get_messages(agent_name=agent_name)
        ids = [m.id for m in all_msgs]
        await self.mark_read(agent_name, ids)

    # ------------------------------------------------------------------
    # 实时通知
    # ------------------------------------------------------------------

    def get_event(self, agent_name: str) -> asyncio.Event:
        """获取 agent 的通知事件（可被 send() 触发）."""
        if agent_name not in self._events:
            self._events[agent_name] = asyncio.Event()
        return self._events[agent_name]

    async def wait_for_message(
        self, agent_name: str, timeout: float = 30.0,
    ) -> TeamMessage | None:
        """阻塞等待新消息（最多 timeout 秒），有新消息时返回."""
        event = self.get_event(agent_name)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        # 获取最新未读消息
        unread = await self.get_unread(agent_name)
        return unread[-1] if unread else None

    # ------------------------------------------------------------------
    # 消息循环检测
    # ------------------------------------------------------------------

    async def check_message_loop(
        self, agent_a: str, agent_b: str, window: int = 10,
    ) -> bool:
        """检测两个 Agent 之间是否存在消息循环（A→B→A→B...）."""
        messages = await self.get_messages(limit=window * 2)
        # 只保留 A↔B 之间的消息
        ab_msgs = [
            m for m in messages
            if {m.from_agent, m.to_agent} == {agent_a, agent_b}
        ]
        if len(ab_msgs) < 4:
            return False
        # 检查最近 4 条是否交替
        recent = ab_msgs[-4:]
        senders = [m.from_agent for m in recent]
        # 模式: A, B, A, B → 循环
        return senders == [agent_a, agent_b, agent_a, agent_b]
