"""cron 工具与无人值守闸门测试."""
from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.clarification import ClarificationMiddleware
from harness.tools.builtins.cron_tool import cron_tool, extract_cron_context
from harness.tools.builtins.lead_tools import build_lead_tools


# ── extract_cron_context ─────────────────────────────────────


def test_extract_context_from_state_and_config():
    ctx = extract_cron_context(
        {"user_id": "tester", "metadata": {"unattended": True}},
        {"configurable": {"thread_id": "t-1"}},
    )
    assert ctx == {"user_id": "tester", "thread_id": "t-1", "unattended": True}


def test_extract_context_defaults():
    ctx = extract_cron_context(None, None)
    assert ctx == {"user_id": "", "thread_id": "", "unattended": False}


def test_extract_context_attended():
    ctx = extract_cron_context({"user_id": "tester", "metadata": {}}, None)
    assert ctx["unattended"] is False


# ── 工具注册 ─────────────────────────────────────────────────


def test_build_lead_tools_includes_cron():
    tools = build_lead_tools(None)
    names = [t.name for t in tools]
    assert "cron" in names
    assert "ask_clarification" in names


# ── cron 工具闸门 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_tool_rejects_when_unattended():
    """无人值守执行中禁止操作定时任务（防递归调度）"""
    tool = cron_tool()
    result = await tool.coroutine(
        action="list",
        config=None,
        state={"user_id": "tester", "metadata": {"unattended": True}},
    )
    assert "无人值守" in result
    assert "递归" in result


@pytest.mark.asyncio
async def test_cron_tool_rejects_without_user():
    tool = cron_tool()
    result = await tool.coroutine(
        action="list",
        config=None,
        state={"metadata": {}},
    )
    assert "无法确定任务归属用户" in result


@pytest.mark.asyncio
async def test_cron_tool_create_validation():
    """create 缺少 name/prompt 或时间字段三选一不满足时返回校验错误（不发请求）"""
    tool = cron_tool()
    state = {"user_id": "tester", "metadata": {}}
    r1 = await tool.coroutine(action="create", cron_expr="0 9 * * *", config=None, state=state)
    assert "name 和 prompt" in r1
    r2 = await tool.coroutine(action="create", name="n", prompt="p", config=None, state=state)
    assert "必须且只能提供一个" in r2
    r3 = await tool.coroutine(
        action="create", name="n", prompt="p",
        cron_expr="0 9 * * *", run_at="2026-07-20T09:00:00",
        config=None, state=state,
    )
    assert "必须且只能提供一个" in r3


@pytest.mark.asyncio
async def test_cron_tool_create_with_delay(monkeypatch):
    """'10 分钟后提醒我' → delay 透传给内部 API（无需知道当前时间）"""
    captured = {}

    async def fake_call_app(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "t-1", "name": "提醒"}

    monkeypatch.setattr("harness.tools.builtins.cron_tool._call_app", fake_call_app)
    tool = cron_tool()
    result = await tool.coroutine(
        action="create", name="提醒", prompt="该休息一下了",
        delay="10m", config=None,
        state={"user_id": "tester", "metadata": {}},
    )
    assert captured["method"] == "POST"
    assert captured["json"]["delay"] == "10m"
    assert "run_at" not in captured["json"]
    assert "cron_expr" not in captured["json"]
    assert "提醒" in result


# ── ClarificationMiddleware 无人值守闸门 ──────────────────────


def _make_request(question: str, unattended: bool) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "ask_clarification",
            "args": {"question": question, "clarification_type": "missing_info"},
            "id": "tc-1",
        },
        tool=None,
        state={"metadata": {"unattended": unattended}, "messages": []},
        runtime=None,
    )


@pytest.mark.asyncio
async def test_clarification_unattended_returns_tool_message():
    """无人值守：不暂停（无 goto=END），返回指示自行决策的 ToolMessage"""
    mw = ClarificationMiddleware()
    req = _make_request("要继续吗？", unattended=True)

    async def handler(request):  # pragma: no cover - 不应被调用
        raise AssertionError("handler should not be called")

    result = await mw.awrap_tool_call(req, handler)
    assert isinstance(result, ToolMessage)
    assert not isinstance(result, Command)
    assert "无人值守" in result.content
    assert result.tool_call_id == "tc-1"


@pytest.mark.asyncio
async def test_clarification_attended_still_interrupts():
    """正常交互执行：保持原有暂停行为（Command(goto=END)）"""
    mw = ClarificationMiddleware()
    req = _make_request("要继续吗？", unattended=False)

    async def handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    result = await mw.awrap_tool_call(req, handler)
    assert isinstance(result, Command)
    assert result.goto == END
