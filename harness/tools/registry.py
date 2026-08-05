"""Tool registry for discovering, registering and binding tools."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

from langchain_core.tools import BaseTool, tool

from harness.config.tool_config import ToolConfig, ToolGroupConfig
from harness.models import ToolGroup
from harness.utils import resolve_variable

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all tools available to agents."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._mcp_tools: dict[str, BaseTool] = {}
        self._categories: dict[str, str] = {}

    # ---- registration ----

    def register(self, tool: BaseTool, category: str = "core") -> None:
        """Register a local tool."""
        self._tools[tool.name] = tool
        self._categories[tool.name] = category

    def register_from_function(
        self,
        func: Callable,
        name: str | None = None,
        description: str = "",
        category: str = "core",
    ) -> BaseTool:
        """Create and register a tool from a callable."""
        decorated = tool(name or func.__name__, description=description)(func)
        self.register(decorated, category=category)
        return decorated

    # ---- lookup ----

    def get_tool(self, name: str) -> BaseTool:
        """Get a tool by name (local or MCP)."""
        if name in self._tools:
            return self._tools[name]
        if name in self._mcp_tools:
            return self._mcp_tools[name]
        raise KeyError(f"tool '{name}' is not registered")

    def has_tool(self, name: str) -> bool:
        """Check whether a tool exists."""
        return name in self._tools or name in self._mcp_tools

    def get_core_tools(self) -> list[BaseTool]:
        """Return all locally registered core tools."""
        return list(self._tools.values())

    def get_tools_by_category(self, category: str) -> list[BaseTool]:
        """Return tools belonging to a category."""
        tools = [
            tool for name, tool in self._tools.items()
            if self._categories.get(name) == category
        ]
        if category == "mcp":
            tools.extend(self._mcp_tools.values())
        return tools

    # ---- MCP loading ----

    async def load_mcp_tools(self, config_path: str = "") -> list[BaseTool]:
        """Load external tools from enabled MCP servers.

        Uses the new ``harness.mcp_integration`` module with persistent sessions,
        cache with mtime invalidation, and OAuth support.
        """
        from harness.mcp_integration import initialize_mcp_tools

        tools = await initialize_mcp_tools(config_path=config_path)
        for t in tools:
            self._mcp_tools[t.name] = t
        return tools

    def get_mcp_tools_sync(self) -> list[BaseTool]:
        """Get cached MCP tools (synchronous, for lazy-init paths)."""
        from harness.mcp import get_cached_mcp_tools

        tools = get_cached_mcp_tools()
        for t in tools:
            self._mcp_tools[t.name] = t
        return tools

    # ---- config-driven tool loading ----

    def load_tools_from_config(self, tools_config: list[ToolConfig] | None) -> list[BaseTool]:
        """Load tools declared in ``config.yaml`` (harness-style).

        Each ``ToolConfig.use`` is a variable path like
        ``harness.tools.search:web_search``. Loaded tools override any
        previously registered tools with the same name.

        Returns the list of tools successfully loaded.
        """
        loaded: list[BaseTool] = []
        if not tools_config:
            return loaded

        for cfg in tools_config:
            try:
                tool = resolve_variable(cfg.use, BaseTool)
                self.register(tool, category=cfg.group)
                loaded.append(tool)
                logger.info(
                    "Loaded tool '%s' from %s (group=%s)",
                    tool.name,
                    cfg.use,
                    cfg.group,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load tool '%s' from %s: %s",
                    cfg.name,
                    cfg.use,
                    exc,
                )

        return loaded

    # ---- plugin loading ----

    def load_plugins_from_config(self, plugins: list[dict[str, Any]] | None) -> None:
        """Load tool plugins declared in configuration (e.g. config.yaml).

        Each entry supports:
            module:     Python module path (required)
            attr:       Attribute / factory name in the module (optional)
            category:   Tool category in the registry (default: "custom")
            is_factory: Whether ``attr`` is a callable returning tool(s)

        When ``attr`` is omitted, the registry attempts to auto-discover
        a ``build_*_tools`` factory in the module.
        """
        if not plugins:
            return

        for plugin in plugins:
            module_name = plugin.get("module")
            if not module_name:
                logger.warning("Plugin entry missing 'module', skipping: %s", plugin)
                continue

            category = plugin.get("category", "custom")
            attr_name = plugin.get("attr")
            is_factory = plugin.get("is_factory", False)

            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                logger.warning("Failed to import plugin module '%s': %s", module_name, exc)
                continue

            # Auto-discover build_*_tools factory when attr is omitted.
            if not attr_name:
                factory_name = None
                for name in dir(module):
                    if name.startswith("build_") and name.endswith("_tools"):
                        factory_name = name
                        break
                if factory_name is None:
                    logger.warning(
                        "Plugin module '%s' has no 'attr' and no build_*_tools factory",
                        module_name,
                    )
                    continue
                attr_name = factory_name
                is_factory = True

            try:
                obj = getattr(module, attr_name)
            except AttributeError:
                logger.warning(
                    "Plugin module '%s' has no attribute '%s'",
                    module_name,
                    attr_name,
                )
                continue

            try:
                tools: list[Any] = []
                if is_factory and callable(obj):
                    result = obj()
                    tools = result if isinstance(result, list) else [result]
                elif callable(obj):
                    # Treat plain callables as tool factories too.
                    tools = [obj]
                else:
                    tools = [obj]

                for t in tools:
                    self.register(t, category=category)
                logger.info(
                    "Loaded plugin tool(s) from %s.%s (category=%s, count=%d)",
                    module_name,
                    attr_name,
                    category,
                    len(tools),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load plugin %s.%s: %s",
                    module_name,
                    attr_name,
                    exc,
                )

    # ---- setup ----

    def setup_tool_groups(
        self,
        tool_groups_config: list[ToolGroupConfig] | None = None,
    ) -> dict[str, ToolGroup]:
        """Return tool groups derived from config and registered categories.

        Args:
            tool_groups_config: Optional list of group definitions from
                ``config.yaml``. When provided, tools are assigned to groups
                based on their registered category matching the group name.
        """
        groups: dict[str, ToolGroup] = {}

        # Build groups from config when available.
        if tool_groups_config:
            for cfg in tool_groups_config:
                tools_in_group = [
                    name for name, category in self._categories.items()
                    if category == cfg.name
                ]
                groups[cfg.name] = ToolGroup(
                    name=cfg.name,
                    description=cfg.description or f"{cfg.name} 工具组",
                    tools=tools_in_group,
                )

        # Auto-create groups for any remaining registered categories.
        for name, category in self._categories.items():
            if category not in groups:
                groups[category] = ToolGroup(
                    name=category,
                    description=f"{category} 工具组",
                    tools=[],
                )
            if name not in groups[category].tools:
                groups[category].tools.append(name)

        # Always include the dynamic MCP group.
        groups["mcp"] = ToolGroup(
            name="mcp",
            description="MCP 外部工具",
            tools=list(self._mcp_tools.keys()),
            dynamic=True,
        )

        return groups

