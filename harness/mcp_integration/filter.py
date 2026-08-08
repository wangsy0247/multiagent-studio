"""Per-agent MCP server 子集过滤.

per-agent ``extensions_config.yaml`` 的 ``mcp_servers`` 是 ``dict[name, bool]``,
语义为黑名单:

- 空 dict → 全部放行 (向后兼容, 未配置子集的 agent 行为不变)
- 显式 ``false`` → 该 agent 禁用此 server 的所有工具
- 缺失 → 放行
"""

from __future__ import annotations

from typing import Any


def filter_mcp_tools_by_agent(
    tools: list[Any], enabled: dict[str, bool] | None
) -> list[Any]:
    """按 per-agent MCP 开关过滤工具.

    依据工具 ``metadata["mcp_server"]``(由 ``_make_session_pool_tool`` 写入);
    无 metadata 的工具(非 MCP 或旧缓存包装)一律放行。
    """
    if not enabled:
        return tools
    return [
        t
        for t in tools
        if enabled.get((getattr(t, "metadata", None) or {}).get("mcp_server", ""), True)
    ]
