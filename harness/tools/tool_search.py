"""tool_search — MCP 工具延迟加载 (harness-aligned).

核心思想: MCP 工具全量绑定会让模型为几十上百个 JSON Schema 付出 token
成本并降低选择准确率。启用后, MCP 工具只暴露名字 (system prompt 清单),
完整 schema 由模型按需通过 ``tool_search`` 工具检索获取。

四个协作组件:

1. ``DeferredToolCatalog`` — 构建期对 MCP 工具建目录, 提供 regex 搜索与
   ``select:`` 精确选取, 并计算 catalog_hash (schema 集合的指纹)。
2. ``tool_search`` 工具 — 返回 ``Command``: ToolMessage 携带匹配工具的完整
   schema (即时通道), 同时把 promoted 记录写入图状态 ``promoted_tools``
   (持久通道, 由 ``merge_promoted_tools`` reducer 合并, hash 漂移时替换)。
3. ``DeferredToolFilterMiddleware`` (middleware/deferred_tool_filter.py) —
   模型绑定层隐藏 "deferred - promoted" 的 schema, 并拦截越权调用。
4. ``get_deferred_prompt_section()`` — system prompt 中的工具名清单。

启用条件: ``config.yaml`` 的 ``tool_search.enabled: true`` 且 MCP 工具数
≥ ``defer_threshold``。由 ``mcp_integration/cache.py`` 在 MCP 工具加载后
调用 ``configure_deferred_tools()`` 完成设置; 未启用时一切为空操作。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.types import Command
from typing_extensions import Annotated

logger = logging.getLogger(__name__)

MAX_RESULTS = 5


class DeferredToolCatalog:
    """延迟加载工具的目录 — 名称索引 + regex 搜索 + schema 指纹."""

    def __init__(self, tools: list[BaseTool] | tuple[BaseTool, ...]):
        # 排序保证 hash 与搜索结果确定性 (prompt 前缀缓存友好)
        self._tools: tuple[BaseTool, ...] = tuple(
            sorted(tools, key=lambda t: t.name)
        )

    @property
    def tools(self) -> tuple[BaseTool, ...]:
        return self._tools

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(t.name for t in self._tools)

    @cached_property
    def hash(self) -> str:
        """目录指纹 — MCP 工具集变化 (重启/增删/schema 变更) 时改变."""
        canon = [
            {"name": t.name, "schema": convert_to_openai_function(t)}
            for t in self._tools
        ]
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def get(self, name: str) -> BaseTool | None:
        for t in self._tools:
            if t.name == name:
                return t
        return None

    def search(self, query: str, max_results: int = MAX_RESULTS) -> list[BaseTool]:
        """三种查询模式 (harness-aligned):

        - ``select:name1,name2`` — 按名精确选取
        - 普通文本 — 作为 regex (编译失败则转义为字面量) 匹配 name+description,
          name 命中权重 2, description 命中权重 1, 取前 max_results 个
        """
        query = (query or "").strip()
        if not query:
            return []

        if query.startswith("select:"):
            wanted = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            picked = [t for n in wanted if (t := self.get(n)) is not None]
            return picked[:max_results]

        try:
            regex = re.compile(query, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(query), re.IGNORECASE)

        scored: list[tuple[int, BaseTool]] = []
        for t in self._tools:
            searchable = f"{t.name} {t.description or ''}"
            if regex.search(searchable):
                scored.append((2 if regex.search(t.name) else 1, t))
        # 分数降序, 同分按名字序 (确定性)
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [t for _, t in scored[:max_results]]


@dataclass
class DeferredToolSetup:
    """延迟加载的全局设置 — catalog + 派生的 tool_search 工具."""

    catalog: DeferredToolCatalog
    tool_search_tool: BaseTool
    deferred_names: frozenset[str] = field(default=frozenset)
    catalog_hash: str = ""

    def __post_init__(self) -> None:
        self.deferred_names = self.catalog.names
        self.catalog_hash = self.catalog.hash


_setup: DeferredToolSetup | None = None
_setup_lock = threading.Lock()


def _load_tool_search_config() -> dict[str, Any]:
    """读取服务器配置的 tool_search 段 (独立实例, 仅初始化时读一次)."""
    try:
        from harness.config.config_manager import ConfigManager
        from harness.config.server_config import server_config_path

        cm = ConfigManager(str(server_config_path()))
        cm.load()
        cfg = cm.get("tool_search")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("Failed to load tool_search config", exc_info=True)
        return {}


def configure_deferred_tools(mcp_tools: list[BaseTool]) -> DeferredToolSetup | None:
    """MCP 工具加载完成后调用 — 按配置决定是否启用延迟加载.

    幂等: 每次 MCP 缓存 (re)initialization 时调用, 目录重建 → hash 变更 →
    各线程已 promote 的记录因 hash 漂移自动失效 (防暴露已变更的工具)。
    """
    global _setup

    cfg = _load_tool_search_config()
    enabled = bool(cfg.get("enabled", False))
    defer_threshold = int(cfg.get("defer_threshold", 10) or 0)
    max_results = int(cfg.get("max_results", MAX_RESULTS) or MAX_RESULTS)

    with _setup_lock:
        if not enabled or len(mcp_tools) < defer_threshold or not mcp_tools:
            if _setup is not None:
                logger.info("tool_search: deferred setup cleared (disabled/below threshold)")
            _setup = None
            return None

        catalog = DeferredToolCatalog(mcp_tools)
        search_tool = _build_tool_search_tool(catalog, max_results)
        _setup = DeferredToolSetup(catalog=catalog, tool_search_tool=search_tool)
        logger.info(
            "tool_search: %d MCP tool(s) deferred (hash=%s, max_results=%d)",
            len(catalog.names), _setup.catalog_hash, max_results,
        )
        return _setup


def get_deferred_setup() -> DeferredToolSetup | None:
    """返回当前延迟加载设置; 未启用为 None (所有消费方据此空操作)."""
    return _setup


def get_tool_search_tool() -> BaseTool | None:
    """返回当前设置下的 tool_search 工具实例 (供 agent 装配时加入 tools)."""
    return _setup.tool_search_tool if _setup is not None else None


def get_deferred_prompt_section() -> str:
    """system prompt 清单段 — 只列名字, 不含 schema (模型无法直接调用)."""
    if _setup is None:
        return ""
    names = sorted(_setup.deferred_names)
    listing = "\n".join(names)
    return (
        "<available-deferred-tools>\n"
        "The following external tools are deferred: you know they exist, but you cannot see their\n"
        "parameter schemas and cannot call them directly. When you need one, call tool_search first\n"
        "(keyword search, or select:<tool_name> for exact selection) to load its full schema, then\n"
        "you can call it normally.\n"
        f"{listing}\n"
        "</available-deferred-tools>"
    )


def promoted_names_from_state(state: Any) -> frozenset[str]:
    """从图状态读取已 promoted 的工具名 (catalog_hash 漂移时视为全部失效)."""
    if _setup is None or not isinstance(state, dict):
        return frozenset()
    promoted = state.get("promoted_tools") or {}
    if promoted.get("catalog_hash") != _setup.catalog_hash:
        return frozenset()
    return frozenset(promoted.get("names", []))


def _build_tool_search_tool(catalog: DeferredToolCatalog, max_results: int) -> BaseTool:
    """构建 tool_search 工具 — 闭包持有 catalog."""

    async def tool_search(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        matched = catalog.search(query, max_results=max_results)
        if not matched:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=(
                                f"No deferred tools matched '{query}'. "
                                "Retry with different keywords, or use select:<tool_name> for exact selection."
                            ),
                            tool_call_id=tool_call_id,
                            name="tool_search",
                        )
                    ]
                }
            )

        schemas = [convert_to_openai_function(t) for t in matched]
        names = [t.name for t in matched]
        content = (
            "The full schemas of the following tools have been loaded; you can now call them directly:\n"
            + json.dumps(schemas, ensure_ascii=False, indent=2, default=str)
        )
        logger.info("tool_search: query=%r → promoted %s", query, names)
        return Command(
            update={
                # 持久通道: 图状态 reducer 合并, middleware 下轮起不再隐藏 schema
                "promoted_tools": {
                    "catalog_hash": catalog.hash,
                    "names": names,
                },
                # 即时通道: 模型在消息历史里直接读到完整 schema
                "messages": [
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        name="tool_search",
                    )
                ],
            }
        )

    return StructuredTool.from_function(
        coroutine=tool_search,
        name="tool_search",
        description=(
            "Search and load the full schemas of deferred external tools (MCP). "
            "Argument query: keywords (regex), or 'select:<tool1>,<tool2>' for exact selection. "
            "After a successful search, these tools can be called directly."
        ),
    )
