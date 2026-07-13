"""TeamMessageBus 单元测试."""

import asyncio
from pathlib import Path

import pytest

from harness.team.message_bus import TeamMessageBus
from harness.team.models import TeamMessage, TeamMessageType


# ── 辅助 ──
def _patch_create_with_dir():
    @classmethod
    def _create_with_dir(cls, base_dir: Path, project_id: str):
        bus = cls.__new__(cls)
        bus._project_id = project_id
        bus._user_id = "default"
        bus._msgs_dir = base_dir
        bus._msgs_dir.mkdir(parents=True, exist_ok=True)
        bus._file = base_dir / "mailbox.jsonl"
        bus._read_cursors = {}
        bus._cursor_file = base_dir / ".read_cursors.json"
        bus._events = {}
        return bus
    TeamMessageBus._create_with_dir = _create_with_dir

_patch_create_with_dir()


@pytest.fixture
def bus(tmp_path):
    """创建使用临时目录的 TeamMessageBus."""
    return TeamMessageBus._create_with_dir(tmp_path, "test_project")


def test_send_and_receive(bus):
    """发送消息并验证接收."""
    async def _test():
        msg = TeamMessage(
            from_agent="lead", to_agent="coder",
            msg_type=TeamMessageType.TEXT,
            content="请修复这个 bug", task_id="task_001",
        )
        await bus.send(msg)

        received = await bus.get_messages(agent_name="coder")
        assert len(received) == 1
        assert received[0].content == "请修复这个 bug"
        assert received[0].from_agent == "lead"

    asyncio.run(_test())


def test_broadcast(bus):
    """广播消息应发给所有人（除发送者）."""
    async def _test():
        msg = TeamMessage(
            from_agent="lead", to_agent=None,
            msg_type=TeamMessageType.BROADCAST,
            content="全体注意",
        )
        await bus.send(msg)

        for name in ("coder", "researcher", "reviewer"):
            received = await bus.get_messages(agent_name=name)
            assert len(received) == 1

        # 发送者不应收到自己的广播
        received = await bus.get_messages(agent_name="lead")
        assert len(received) == 0

    asyncio.run(_test())


def test_unread_tracking(bus):
    """未读消息追踪."""
    async def _test():
        msg1 = TeamMessage(from_agent="lead", to_agent="coder", content="msg1")
        msg2 = TeamMessage(from_agent="lead", to_agent="coder", content="msg2")
        await bus.send(msg1)
        await bus.send(msg2)

        unread = await bus.get_unread("coder")
        assert len(unread) == 2

        await bus.mark_read("coder", [msg1.id, msg2.id])
        unread = await bus.get_unread("coder")
        assert len(unread) == 0

    asyncio.run(_test())


def test_message_persistence(bus, tmp_path):
    """消息应持久化到 JSONL 文件."""
    async def _test():
        msg = TeamMessage(from_agent="lead", to_agent="coder", content="持久化测试")
        await bus.send(msg)
        assert bus._file.exists()

        bus2 = TeamMessageBus._create_with_dir(tmp_path, "test_project")
        received = await bus2.get_messages(agent_name="coder")
        assert len(received) == 1
        assert received[0].content == "持久化测试"

    asyncio.run(_test())


def test_message_loop_detection(bus):
    """检测消息循环."""
    async def _test():
        for _ in range(4):
            await bus.send(TeamMessage(from_agent="A", to_agent="B", content="loop"))
            await bus.send(TeamMessage(from_agent="B", to_agent="A", content="loop"))

        is_loop = await bus.check_message_loop("A", "B", window=10)
        assert is_loop is True

    asyncio.run(_test())


def test_no_loop_when_normal(bus):
    """正常消息不应被误判为循环."""
    async def _test():
        await bus.send(TeamMessage(from_agent="A", to_agent="B", content="hi"))
        await bus.send(TeamMessage(from_agent="B", to_agent="C", content="ack"))
        await bus.send(TeamMessage(from_agent="C", to_agent="A", content="done"))

        is_loop = await bus.check_message_loop("A", "B", window=10)
        assert is_loop is False

    asyncio.run(_test())
