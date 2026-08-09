"""session_search 工具测试 — 注册、输出格式、小模型总结与降级."""
from __future__ import annotations

import pytest

import harness.tools.builtins.session_search_tool as sst
from harness.tools.builtins.lead_tools import build_lead_tools
from harness.tools.builtins.session_search_tool import (
    _format_results,
    _summarize_all,
    create_session_search_tool,
)


def _sessions():
    return [
        {"thread_id": "t1", "title": "部署讨论", "matches": [{"role": "ai"}],
         "transcript": "[HUMAN]: 怎么部署\n[AI]: 用 helm"},
        {"thread_id": "t2", "title": "桂林行", "matches": [{"role": "human"}],
         "transcript": "[HUMAN]: 桂林好玩吗"},
    ]


# ── 工具注册 ─────────────────────────────────────────────────


def test_build_lead_tools_includes_session_search():
    names = [t.name for t in build_lead_tools(None)]
    assert "session_search" in names


# ── 输出格式 ─────────────────────────────────────────────────


def test_format_prefers_summary():
    out = _format_results(_sessions(), ["部署相关总结", None])
    assert "部署相关总结" in out
    # 第二条无总结 → 降级为原始 transcript
    assert "桂林好玩吗" in out
    assert "thread_id: t1" in out


def test_format_without_summaries_uses_transcript():
    out = _format_results(_sessions(), None)
    assert "用 helm" in out
    assert "桂林好玩吗" in out


# ── 小模型总结 ───────────────────────────────────────────────


class _FakeResp:
    content = "这是总结内容"


class _FakeLLM:
    async def ainvoke(self, messages):
        return _FakeResp()


class _FailLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_summarize_all_success(monkeypatch):
    monkeypatch.setattr(sst, "_resolve_summary_config", lambda uid: ("k", "u", "m"))
    monkeypatch.setattr(sst, "_get_summary_llm", lambda *a: _FakeLLM())
    out = await _summarize_all(_sessions(), "部署", "tester")
    assert out == ["这是总结内容", "这是总结内容"]


@pytest.mark.asyncio
async def test_summarize_all_no_credentials_skips(monkeypatch):
    monkeypatch.setattr(sst, "_resolve_summary_config", lambda uid: ("", "u", "m"))
    out = await _summarize_all(_sessions(), "部署", "tester")
    assert out == [None, None]


@pytest.mark.asyncio
async def test_summarize_all_failure_returns_none(monkeypatch):
    monkeypatch.setattr(sst, "_resolve_summary_config", lambda uid: ("k", "u", "m"))
    monkeypatch.setattr(sst, "_get_summary_llm", lambda *a: _FailLLM())
    out = await _summarize_all(_sessions(), "部署", "tester")
    assert out == [None, None]  # 失败降级，由 _format_results 回退 transcript


# ── 工具端到端（mock HTTP 与总结）─────────────────────────────


@pytest.mark.asyncio
async def test_tool_end_to_end(monkeypatch):
    async def fake_call_app(path, payload):
        assert payload["username"] == "tester"
        assert payload["exclude_thread_id"] == "cur-thread"
        return {"sessions": _sessions()}

    monkeypatch.setattr(sst, "_call_app", fake_call_app)

    async def fake_summarize(sessions, query, user_id):
        return ["总结A", "总结B"]

    monkeypatch.setattr(sst, "_summarize_all", fake_summarize)

    tool = create_session_search_tool()
    result = await tool.coroutine(
        query="部署",
        config={"configurable": {"thread_id": "cur-thread"}},
        state={"user_id": "tester"},
    )
    assert "总结A" in result and "总结B" in result
    assert "部署讨论" in result


@pytest.mark.asyncio
async def test_tool_requires_user_id():
    tool = create_session_search_tool()
    result = await tool.coroutine(query="x", config=None, state={})
    assert "cannot determine the current user" in result


@pytest.mark.asyncio
async def test_tool_no_hits(monkeypatch):
    async def fake_call_app(path, payload):
        return {"sessions": []}

    monkeypatch.setattr(sst, "_call_app", fake_call_app)
    tool = create_session_search_tool()
    result = await tool.coroutine(query="x", config=None, state={"user_id": "tester"})
    assert "No matching messages found" in result
