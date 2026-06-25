"""Tool registry for discovering, registering and binding tools."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

from langchain_core.tools import BaseTool, tool

from harness.config import HarnessConfig
from harness.models import ToolGroup
from harness.services.sandbox import SandboxService

from .abacus import build_abacus_tools
from .code import CodeTools
from .core import build_core_tools
from .files import build_file_tools
from .mcp_adapter import load_mcp_tools_from_config
from .search import build_search_tools
from .weather import build_weather_tools

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all tools available to agents."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._mcp_tools: dict[str, BaseTool] = {}
        self._categories: dict[str, str] = {}
        self._sandbox: SandboxService | None = None
        self._workspace: str = "."
        self._thread_id: str | None = None

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
        return [
            tool for name, tool in self._tools.items()
            if self._categories.get(name) == category
        ]

    # ---- MCP loading ----

    async def load_mcp_tools(self, config_path: str) -> list[BaseTool]:
        """Load external tools from an MCP configuration file."""
        tools = await load_mcp_tools_from_config(config_path)
        for t in tools:
            self._mcp_tools[t.name] = t
        return tools

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

    def setup_tool_groups(self) -> dict[str, ToolGroup]:
        """Return predefined tool groups."""
        return {
            "search": ToolGroup(
                name="search",
                description="搜索工具组",
                tools=["web_search", "arxiv_search", "paper_search"],
            ),
            "code": ToolGroup(
                name="code",
                description="代码执行工具组",
                tools=["python", "bash", "execute_code"],
            ),
            "files": ToolGroup(
                name="files",
                description="文件操作工具组",
                tools=["file_read", "file_write", "list_files"],
            ),
            "data": ToolGroup(
                name="data",
                description="数据分析工具组",
                tools=["python", "chart_generate", "csv_process"],
            ),
            "abacus": ToolGroup(
                name="abacus",
                description="Abacus 材料计算工具组",
                tools=["generate_abacus_input", "submit_abacus_job"],
            ),
            "weather": ToolGroup(
                name="weather",
                description="天气查询工具组",
                tools=["weather_search"],
            ),
            "mcp": ToolGroup(
                name="mcp",
                description="MCP 外部工具",
                tools=list(self._mcp_tools.keys()),
                dynamic=True,
            ),
        }

    def bind_context(
        self,
        sandbox: SandboxService | None = None,
        workspace: str = ".",
        thread_id: str | None = None,
    ) -> "ToolRegistry":
        """Bind runtime context (sandbox + workspace) to execution tools."""
        self._sandbox = sandbox
        self._workspace = workspace
        self._thread_id = thread_id
        # Re-register code tools with the provided sandbox.
        code_tools = CodeTools(sandbox=sandbox).get_tools()
        for t in code_tools:
            self.register(t, category="code")
        # Re-register file tools with the provided workspace.
        file_tools = build_file_tools(workspace=workspace)
        for t in file_tools:
            self.register(t, category="files")
        return self

    def initialize_defaults(self, config: HarnessConfig | None = None) -> "ToolRegistry":
        """Register the built-in tool sets."""
        for tool in build_core_tools():
            self.register(tool, category="core")
        for tool in build_search_tools():
            self.register(tool, category="search")
        for tool in CodeTools(sandbox=self._sandbox).get_tools():
            self.register(tool, category="code")
        for tool in build_file_tools(workspace=self._workspace):
            self.register(tool, category="files")
        for tool in build_abacus_tools():
            self.register(tool, category="abacus")
        for tool in build_weather_tools():
            self.register(tool, category="weather")
        return self
