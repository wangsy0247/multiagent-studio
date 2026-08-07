"""Load MCP tools using langchain-mcp-adapters with persistent sessions."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_config

from harness.config.extensions_config import ExtensionsConfig
from harness.mcp_integration.client import build_servers_config
from harness.mcp_integration.oauth import (
    build_oauth_tool_interceptor,
    get_initial_oauth_headers,
)
from harness.mcp_integration.session_pool import get_session_pool
from harness.utils import resolve_variable

logger = logging.getLogger(__name__)


def _extract_thread_id(runtime: Any | None = None) -> str:
    """Extract thread_id from runtime context, RunnableConfig, or LangGraph config."""
    if runtime is not None:
        # Try ToolRuntime or similar object with context/config
        if hasattr(runtime, "context"):
            ctx = runtime.context
            if isinstance(ctx, dict) and "thread_id" in ctx:
                return str(ctx["thread_id"])
        if hasattr(runtime, "config"):
            cfg = runtime.config
            if isinstance(cfg, dict):
                tid = (
                    cfg.get("configurable", {})
                    if isinstance(cfg.get("configurable"), dict)
                    else {}
                ).get("thread_id")
                if tid is not None:
                    return str(tid)

    # Fallback to LangGraph config
    try:
        lg_config = get_config()
        tid = (
            lg_config.get("configurable", {})
            if isinstance(lg_config.get("configurable"), dict)
            else {}
        ).get("thread_id")
        return str(tid or "default")
    except RuntimeError:
        return "default"


def _convert_call_tool_result(call_tool_result: Any) -> Any:
    """Convert an MCP CallToolResult to the LangChain ``content_and_artifact`` format."""
    from langchain_core.messages import ToolMessage
    from langchain_core.messages.content import (
        create_file_block,
        create_image_block,
        create_text_block,
    )
    from langchain_core.tools import ToolException
    from mcp.types import (
        EmbeddedResource,
        ImageContent,
        ResourceLink,
        TextContent,
        TextResourceContents,
    )

    # Pass ToolMessage through directly (interceptor short-circuit).
    if isinstance(call_tool_result, ToolMessage):
        return call_tool_result, None

    # Pass LangGraph Command through directly.
    try:
        from langgraph.types import Command

        if isinstance(call_tool_result, Command):
            return call_tool_result, None
    except ImportError:
        pass

    # Convert MCP content blocks to LangChain content blocks.
    lc_content = []
    for item in call_tool_result.content:
        if isinstance(item, TextContent):
            lc_content.append(create_text_block(text=item.text))
        elif isinstance(item, ImageContent):
            lc_content.append(
                create_image_block(base64=item.data, mime_type=item.mimeType)
            )
        elif isinstance(item, ResourceLink):
            mime = item.mimeType or None
            if mime and mime.startswith("image/"):
                lc_content.append(
                    create_image_block(url=str(item.uri), mime_type=mime)
                )
            else:
                lc_content.append(
                    create_file_block(url=str(item.uri), mime_type=mime)
                )
        elif isinstance(item, EmbeddedResource):
            from mcp.types import BlobResourceContents

            res = item.resource
            if isinstance(res, TextResourceContents):
                lc_content.append(create_text_block(text=res.text))
            elif isinstance(res, BlobResourceContents):
                mime = res.mimeType or None
                if mime and mime.startswith("image/"):
                    lc_content.append(
                        create_image_block(base64=res.blob, mime_type=mime)
                    )
                else:
                    lc_content.append(
                        create_file_block(base64=res.blob, mime_type=mime)
                    )
            else:
                lc_content.append(create_text_block(text=str(res)))
        else:
            lc_content.append(create_text_block(text=str(item)))

    if call_tool_result.isError:
        error_parts = [
            item["text"]
            for item in lc_content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        raise ToolException(
            "\n".join(error_parts) if error_parts else str(lc_content)
        )

    artifact = None
    if call_tool_result.structuredContent is not None:
        artifact = {"structured_content": call_tool_result.structuredContent}

    return lc_content, artifact


def _make_session_pool_tool(
    tool: BaseTool,
    server_name: str,
    connection: dict[str, Any],
    tool_interceptors: list[Any] | None = None,
) -> BaseTool:
    """Wrap an MCP tool so it reuses a persistent session from the pool.

    Replaces the per-call session creation with pool-managed sessions scoped
    by ``(server_name, thread_id)``.
    """
    original_name = tool.name
    prefix = f"{server_name}_"
    if original_name.startswith(prefix):
        original_name = original_name[len(prefix):]

    pool = get_session_pool()

    async def call_with_persistent_session(
        runtime: Any | None = None,
        **arguments: Any,
    ) -> Any:
        thread_id = _extract_thread_id(runtime)
        session = await pool.get_session(server_name, thread_id, connection)

        if tool_interceptors:
            from langchain_mcp_adapters.interceptors import MCPToolCallRequest

            async def base_handler(request: MCPToolCallRequest) -> Any:
                # OAuth interceptor 刷新出的新 token 在 request.headers 里 —
                # 与已建 session 的创建时 headers 不一致时就地重建 session,
                # 否则初始 token 过期后所有调用持续 401 到进程重启
                live_session = await pool.get_session(
                    server_name, thread_id, connection,
                    headers=dict(request.headers or {}),
                )
                return await live_session.call_tool(request.name, request.args)

            handler = base_handler
            for interceptor in reversed(tool_interceptors):
                outer = handler

                async def wrapped(
                    req: Any, _i: Any = interceptor, _h: Any = outer
                ) -> Any:
                    return await _i(req, _h)

                handler = wrapped

            request = MCPToolCallRequest(
                name=original_name,
                args=arguments,
                server_name=server_name,
                runtime=runtime,
            )
            call_tool_result = await handler(request)
        else:
            call_tool_result = await session.call_tool(original_name, arguments)

        return _convert_call_tool_result(call_tool_result)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=call_with_persistent_session,
        response_format="content_and_artifact",
        metadata=tool.metadata,
    )


async def get_mcp_tools(config_path: str = "") -> list[BaseTool]:
    """Get all tools from enabled MCP servers.

    Tools are wrapped with persistent-session logic so that consecutive
    calls within the same thread reuse the same MCP session.

    Args:
        config_path: Path to ``extensions_config.json``. Empty = auto-detect.

    Returns:
        List of LangChain tools from all enabled MCP servers.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning(
            "langchain-mcp-adapters not installed. "
            "Install it to enable MCP tools: pip install langchain-mcp-adapters"
        )
        return []

    # Always read the latest configuration from disk.
    extensions_config = ExtensionsConfig.from_file(config_path or None)
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("No enabled MCP servers configured")
        return []

    try:
        logger.info(
            "Initializing MCP client with %d server(s)", len(servers_config)
        )

        # Inject initial OAuth headers for server connections.
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(
                    servers_config[server_name].get("headers", {})
                )
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        tool_interceptors: list[Any] = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # Load custom interceptors declared in extensions_config.json
        # Format: "mcpInterceptors": ["pkg.module:builder_func", ...]
        raw_interceptor_paths = extensions_config.model_extra or {}
        raw_interceptor_paths = raw_interceptor_paths.get(
            "mcpInterceptors"
        ) or extensions_config.model_extra.get("mcp_interceptors")
        if isinstance(raw_interceptor_paths, str):
            raw_interceptor_paths = [raw_interceptor_paths]
        elif not isinstance(raw_interceptor_paths, list):
            if raw_interceptor_paths is not None:
                logger.warning(
                    "mcpInterceptors must be a list of strings, "
                    "got %s; skipping",
                    type(raw_interceptor_paths).__name__,
                )
            raw_interceptor_paths = []
        if raw_interceptor_paths:
            for interceptor_path in raw_interceptor_paths:
                try:
                    builder = resolve_variable(interceptor_path)
                    interceptor = builder()
                    if callable(interceptor):
                        tool_interceptors.append(interceptor)
                        logger.info(
                            "Loaded MCP interceptor: %s", interceptor_path
                        )
                    elif interceptor is not None:
                        logger.warning(
                            "Builder %s returned non-callable %s; skipping",
                            interceptor_path,
                            type(interceptor).__name__,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to load MCP interceptor %s: %s",
                        interceptor_path,
                        exc,
                        exc_info=True,
                    )

        client = MultiServerMCPClient(
            servers_config,
            tool_interceptors=tool_interceptors,
            tool_name_prefix=True,
        )

        # Get all tools from all servers (discovers tool definitions via
        # temporary sessions – the persistent-session wrapping is applied below).
        tools = await client.get_tools()
        logger.info(
            "Successfully loaded %d tool(s) from MCP servers", len(tools)
        )

        # Wrap each tool with persistent-session logic.
        wrapped_tools: list[BaseTool] = []
        for tool in tools:
            tool_server: str | None = None
            for name in servers_config:
                if tool.name.startswith(f"{name}_"):
                    tool_server = name
                    break

            if tool_server is not None:
                wrapped_tools.append(
                    _make_session_pool_tool(
                        tool,
                        tool_server,
                        servers_config[tool_server],
                        tool_interceptors,
                    )
                )
            else:
                wrapped_tools.append(tool)

        return wrapped_tools

    except Exception as exc:
        logger.error("Failed to load MCP tools: %s", exc, exc_info=True)
        return []
