"""Per-agent MCP/skill 子集过滤 (extensions_config.yaml 黑名单语义)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.mcp_integration.filter import filter_mcp_tools_by_agent
from harness.skills.filter import (
    filter_skills_by_agent,
    filter_skills_by_current_context,
    set_current_enabled_skills,
)


def _tool(name: str, server: str | None = None):
    metadata = {"mcp_server": server} if server else None
    return SimpleNamespace(name=name, metadata=metadata)


def _skill(name: str):
    return SimpleNamespace(name=name)


class TestMcpFilter:
    def test_empty_dict_allows_all(self):
        tools = [_tool("github_pr", "github"), _tool("web_search")]
        assert filter_mcp_tools_by_agent(tools, {}) == tools
        assert filter_mcp_tools_by_agent(tools, None) == tools

    def test_explicit_false_removes_server_tools(self):
        tools = [_tool("github_pr", "github"), _tool("github_issue", "github"),
                 _tool("brave_search", "brave-search"), _tool("web_search")]
        result = filter_mcp_tools_by_agent(tools, {"github": False})
        assert [t.name for t in result] == ["brave_search", "web_search"]

    def test_missing_entry_allows(self):
        tools = [_tool("github_pr", "github")]
        result = filter_mcp_tools_by_agent(tools, {"other": False})
        assert len(result) == 1

    def test_tool_without_metadata_untouched(self):
        tools = [_tool("web_search"), _tool("file_read")]
        result = filter_mcp_tools_by_agent(tools, {"github": False})
        assert result == tools

    def test_explicit_true_keeps(self):
        tools = [_tool("github_pr", "github")]
        result = filter_mcp_tools_by_agent(tools, {"github": True})
        assert len(result) == 1


class TestSkillFilter:
    def test_empty_dict_allows_all(self):
        skills = [_skill("a"), _skill("b")]
        assert filter_skills_by_agent(skills, {}) == skills
        assert filter_skills_by_agent(skills, None) == skills

    def test_explicit_false_removes(self):
        skills = [_skill("a"), _skill("b"), _skill("c")]
        result = filter_skills_by_agent(skills, {"b": False})
        assert [s.name for s in result] == ["a", "c"]

    def test_missing_entry_allows(self):
        skills = [_skill("a")]
        assert len(filter_skills_by_agent(skills, {"x": False})) == 1

    def test_contextvar_inheritance(self):
        """subagent 经 contextvar 继承 parent 的黑名单."""
        set_current_enabled_skills({"a": False})
        try:
            result = filter_skills_by_current_context([_skill("a"), _skill("b")])
            assert [s.name for s in result] == ["b"]
        finally:
            set_current_enabled_skills({})
        assert len(filter_skills_by_current_context([_skill("a")])) == 1


class TestOrchestratorAssembly:
    """成员工具装配: eff.enabled_mcp_servers 作用于 mcp 组."""

    def test_member_mcp_group_filtered(self):
        eff = SimpleNamespace(
            tool_groups=["mcp", "search"],
            enabled_mcp_servers={"github": False},
        )
        registry = SimpleNamespace(
            get_tools_by_category=lambda g: {
                "mcp": [_tool("github_pr", "github"), _tool("brave_find", "brave-search")],
                "search": [_tool("web_search")],
            }[g]
        )
        tools: list = []
        for group in eff.tool_groups:
            group_tools = registry.get_tools_by_category(group)
            if group == "mcp":
                group_tools = filter_mcp_tools_by_agent(
                    group_tools, eff.enabled_mcp_servers,
                )
            tools.extend(group_tools)
        assert [t.name for t in tools] == ["brave_find", "web_search"]
