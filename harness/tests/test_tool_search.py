"""Tests for tool_search deferred MCP tool loading (harness-aligned)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from harness.models import merge_promoted_tools
from harness.tools import tool_search as ts
from harness.tools.tool_search import (
    DeferredToolCatalog,
    DeferredToolSetup,
    configure_deferred_tools,
    get_deferred_prompt_section,
    get_deferred_setup,
    promoted_names_from_state,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, description: str = "") -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda x: x, name=name, description=description or f"desc of {name}",
    )


def _fake_config(enabled: bool = True, defer_threshold: int = 0, max_results: int = 5):
    return {
        "enabled": enabled,
        "defer_threshold": defer_threshold,
        "max_results": max_results,
    }


@pytest.fixture(autouse=True)
def _reset_setup(monkeypatch):
    """每个测试前后清空全局 setup, 并用 fake config 替代 config.yaml 读取."""
    monkeypatch.setattr(ts, "_load_tool_search_config", lambda: _fake_config())
    ts._setup = None
    yield
    ts._setup = None


@pytest.fixture
def mcp_tools() -> list[StructuredTool]:
    return [
        _make_tool("github_create_issue", "Create an issue in a GitHub repository"),
        _make_tool("github_list_repos", "List repositories of a user"),
        _make_tool("postgres_query", "Run a SQL query against Postgres"),
        _make_tool("slack_send_message", "Send a message to a Slack channel"),
        _make_tool("github_get_issue", "Get details of a single issue"),
        _make_tool("filesystem_read", "Read a file from disk"),
    ]


# ---------------------------------------------------------------------------
# DeferredToolCatalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_names_and_deterministic_order(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        assert catalog.names == frozenset(t.name for t in mcp_tools)
        names = [t.name for t in catalog.tools]
        assert names == sorted(names)

    def test_hash_deterministic_and_sensitive_to_schema(self, mcp_tools):
        h1 = DeferredToolCatalog(mcp_tools).hash
        h2 = DeferredToolCatalog(list(reversed(mcp_tools))).hash
        assert h1 == h2  # 顺序无关

        changed = [_make_tool(t.name, t.description + " changed") for t in mcp_tools]
        h3 = DeferredToolCatalog(changed).hash
        assert h3 != h1  # schema 变化 → hash 漂移

    def test_search_select_exact(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        got = catalog.search("select:github_create_issue,postgres_query")
        assert [t.name for t in got] == ["github_create_issue", "postgres_query"]

    def test_search_select_ignores_unknown(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        got = catalog.search("select:github_create_issue,no_such_tool")
        assert [t.name for t in got] == ["github_create_issue"]

    def test_search_regex_name_outranks_description(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        got = catalog.search("issue")
        names = [t.name for t in got]
        # name 命中 (github_create_issue/github_get_issue) 排在 description 命中之前
        assert names[0].startswith("github_")
        assert "github_create_issue" in names and "github_get_issue" in names

    def test_search_invalid_regex_falls_back_to_literal(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        got = catalog.search("github_[")  # 非法 regex → 字面量, 无匹配
        assert got == []

    def test_search_max_results_cap(self, mcp_tools):
        catalog = DeferredToolCatalog(mcp_tools)
        got = catalog.search("github|postgres|slack|filesystem", max_results=3)
        assert len(got) == 3

    def test_search_empty_query(self, mcp_tools):
        assert DeferredToolCatalog(mcp_tools).search("") == []


# ---------------------------------------------------------------------------
# merge_promoted_tools reducer
# ---------------------------------------------------------------------------


class TestMergePromotedTools:
    def test_empty_right_keeps_left(self):
        left = {"catalog_hash": "abc", "names": ["a"]}
        assert merge_promoted_tools(left, None) == left

    def test_same_hash_unions_deduped(self):
        left = {"catalog_hash": "abc", "names": ["a", "b"]}
        right = {"catalog_hash": "abc", "names": ["b", "c"]}
        merged = merge_promoted_tools(left, right)
        assert merged == {"catalog_hash": "abc", "names": ["a", "b", "c"]}

    def test_hash_drift_replaces(self):
        left = {"catalog_hash": "old", "names": ["a", "b"]}
        right = {"catalog_hash": "new", "names": ["c"]}
        merged = merge_promoted_tools(left, right)
        assert merged == {"catalog_hash": "new", "names": ["c"]}

    def test_none_left(self):
        right = {"catalog_hash": "h", "names": ["x"]}
        assert merge_promoted_tools(None, right) == right


# ---------------------------------------------------------------------------
# configure_deferred_tools
# ---------------------------------------------------------------------------


class TestConfigure:
    def test_enabled_builds_setup(self, mcp_tools):
        setup = configure_deferred_tools(mcp_tools)
        assert setup is not None
        assert get_deferred_setup() is setup
        assert setup.deferred_names == frozenset(t.name for t in mcp_tools)
        assert setup.tool_search_tool.name == "tool_search"

    def test_disabled_returns_none(self, mcp_tools, monkeypatch):
        monkeypatch.setattr(
            ts, "_load_tool_search_config", lambda: _fake_config(enabled=False)
        )
        assert configure_deferred_tools(mcp_tools) is None
        assert get_deferred_setup() is None

    def test_below_threshold_returns_none(self, mcp_tools, monkeypatch):
        monkeypatch.setattr(
            ts, "_load_tool_search_config", lambda: _fake_config(defer_threshold=100)
        )
        assert configure_deferred_tools(mcp_tools) is None
        assert get_deferred_setup() is None

    def test_reconfigure_changes_hash(self, mcp_tools):
        s1 = configure_deferred_tools(mcp_tools)
        s2 = configure_deferred_tools(mcp_tools + [_make_tool("new_tool")])
        assert s1.catalog_hash != s2.catalog_hash

    def test_prompt_section_lists_names_only(self, mcp_tools):
        configure_deferred_tools(mcp_tools)
        section = get_deferred_prompt_section()
        assert "<available-deferred-tools>" in section
        assert "github_create_issue" in section
        assert "parameters" not in section  # 不含 schema

    def test_prompt_section_empty_when_disabled(self):
        assert get_deferred_prompt_section() == ""


# ---------------------------------------------------------------------------
# tool_search 工具 (Command 双通道)
# ---------------------------------------------------------------------------


class TestToolSearchTool:
    @pytest.mark.asyncio
    async def test_returns_command_with_schemas_and_promotion(self, mcp_tools):
        configure_deferred_tools(mcp_tools)
        tool = get_deferred_setup().tool_search_tool
        cmd = await tool.coroutine(query="select:github_create_issue", tool_call_id="tc-1")

        assert set(cmd.update["promoted_tools"]["names"]) == {"github_create_issue"}
        assert cmd.update["promoted_tools"]["catalog_hash"] == get_deferred_setup().catalog_hash

        msgs = cmd.update["messages"]
        assert len(msgs) == 1 and msgs[0].tool_call_id == "tc-1"
        assert "github_create_issue" in msgs[0].content
        assert "parameters" in msgs[0].content  # 完整 schema 在 ToolMessage 里

    @pytest.mark.asyncio
    async def test_no_match_returns_message_without_promotion(self, mcp_tools):
        configure_deferred_tools(mcp_tools)
        tool = get_deferred_setup().tool_search_tool
        cmd = await tool.coroutine(query="zzz_no_match", tool_call_id="tc-2")
        assert "promoted_tools" not in cmd.update
        assert "未找到" in cmd.update["messages"][0].content

    @pytest.mark.asyncio
    async def test_max_results_respected(self, mcp_tools, monkeypatch):
        monkeypatch.setattr(
            ts, "_load_tool_search_config", lambda: _fake_config(max_results=2)
        )
        configure_deferred_tools(mcp_tools)
        tool = get_deferred_setup().tool_search_tool
        cmd = await tool.coroutine(query="github", tool_call_id="tc-3")
        assert len(cmd.update["promoted_tools"]["names"]) <= 2


# ---------------------------------------------------------------------------
# promoted_names_from_state (hash 漂移防护)
# ---------------------------------------------------------------------------


class TestPromotedFromState:
    def test_reads_names_when_hash_matches(self, mcp_tools):
        setup = configure_deferred_tools(mcp_tools)
        state = {"promoted_tools": {"catalog_hash": setup.catalog_hash, "names": ["a"]}}
        assert promoted_names_from_state(state) == frozenset({"a"})

    def test_hash_drift_invalidates(self, mcp_tools):
        configure_deferred_tools(mcp_tools)
        state = {"promoted_tools": {"catalog_hash": "stale", "names": ["a"]}}
        assert promoted_names_from_state(state) == frozenset()

    def test_no_setup_returns_empty(self):
        assert promoted_names_from_state({"promoted_tools": {"catalog_hash": "h", "names": ["a"]}}) == frozenset()


# ---------------------------------------------------------------------------
# DeferredToolFilterMiddleware
# ---------------------------------------------------------------------------


def _model_request(tools: list[Any], state: dict):
    req = SimpleNamespace(tools=tools, state=state)

    def override(**kwargs):
        new = SimpleNamespace(tools=kwargs.get("tools", req.tools), state=req.state)
        new.override = req.override
        return new

    req.override = override
    return req


class TestDeferredToolFilterMiddleware:
    @pytest.mark.asyncio
    async def test_hides_deferred_keeps_promoted(self, mcp_tools):
        from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware

        setup = configure_deferred_tools(mcp_tools)
        core = _make_tool("core_tool")
        state = {
            "promoted_tools": {
                "catalog_hash": setup.catalog_hash,
                "names": ["github_create_issue"],
            }
        }
        req = _model_request([core] + mcp_tools, state)

        captured: dict[str, Any] = {}

        async def handler(r):
            captured["tools"] = [t.name for t in r.tools]
            return "ok"

        mw = DeferredToolFilterMiddleware()
        assert await mw.awrap_model_call(req, handler) == "ok"
        # 保留: core + tool_search 无关项 + promoted 的 github_create_issue
        assert "core_tool" in captured["tools"]
        assert "github_create_issue" in captured["tools"]
        # 隐藏: 其余 deferred
        assert "postgres_query" not in captured["tools"]
        assert "slack_send_message" not in captured["tools"]

    @pytest.mark.asyncio
    async def test_noop_when_no_setup(self, mcp_tools):
        from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware

        req = _model_request(mcp_tools, {})

        async def handler(r):
            return [t.name for t in r.tools]

        mw = DeferredToolFilterMiddleware()
        got = await mw.awrap_model_call(req, handler)
        assert got == [t.name for t in mcp_tools]  # 不过滤

    @pytest.mark.asyncio
    async def test_blocks_unpromoted_call(self, mcp_tools):
        from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware

        configure_deferred_tools(mcp_tools)
        req = SimpleNamespace(
            tool_call={"name": "postgres_query", "id": "call-1"}, state={},
        )

        async def handler(r):  # pragma: no cover - 不应被调用
            raise AssertionError("unpromoted tool must not execute")

        mw = DeferredToolFilterMiddleware()
        msg = await mw.awrap_tool_call(req, handler)
        assert msg.status == "error"
        assert "tool_search" in msg.content
        assert msg.tool_call_id == "call-1"

    @pytest.mark.asyncio
    async def test_allows_promoted_call(self, mcp_tools):
        from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware

        setup = configure_deferred_tools(mcp_tools)
        state = {
            "promoted_tools": {
                "catalog_hash": setup.catalog_hash,
                "names": ["postgres_query"],
            }
        }
        req = SimpleNamespace(
            tool_call={"name": "postgres_query", "id": "call-2"}, state=state,
        )

        async def handler(r):
            return "executed"

        mw = DeferredToolFilterMiddleware()
        assert await mw.awrap_tool_call(req, handler) == "executed"

    @pytest.mark.asyncio
    async def test_non_deferred_tool_passes_through(self, mcp_tools):
        from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware

        configure_deferred_tools(mcp_tools)
        req = SimpleNamespace(tool_call={"name": "core_tool", "id": "c"}, state={})

        async def handler(r):
            return "executed"

        mw = DeferredToolFilterMiddleware()
        assert await mw.awrap_tool_call(req, handler) == "executed"
