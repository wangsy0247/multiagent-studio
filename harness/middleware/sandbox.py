"""SandboxMiddleware — provide Docker container isolation for code execution.

Supports ``lazy_init=True`` (DeerFlow-compatible): defer container creation until
the first sandbox-wrapped tool (bash/python/execute_code) is actually invoked.
"""
from __future__ import annotations

import contextvars
import logging

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState
from harness.services.sandbox import SandboxService

logger = logging.getLogger(__name__)

SANDBOX_WRAPPED_TOOLS = {"bash", "python", "execute_code"}

# ── thread-safe context (fixes race condition with shared instance) ──
_thread_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sandbox_thread_id", default=""
)
_workspace_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sandbox_workspace", default=""
)


class SandboxMiddleware(HarnessAgentMiddleware):
    """Acquire a sandbox container and wrap execution tools.

    - ``lazy_init=False`` (default): container created in ``abefore_agent``.
    - ``lazy_init=True``: container created lazily on first sandboxed tool call.
    """

    name = "sandbox"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._service: SandboxService | None = None
        self._lazy_init: bool = self.config.get("lazy_init", False)

    def _get_service(self) -> SandboxService:
        if self._service is None:
            self._service = SandboxService(
                image=self.config.get("sandbox_image", "python:3.11-slim"),
                mem_limit=self.config.get("mem_limit", "512m"),
                cpu_quota=self.config.get("cpu_quota", 100000),
            )
        return self._service

    # ------------------------------------------------------------------
    # abefore_agent
    # ------------------------------------------------------------------

    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Initialize sandbox — eager or lazy depending on config."""
        thread_id = state.get("thread_id", "unknown")
        workspace = state.get("workspace", ".")

        if self._lazy_init:
            # Store context for later lazy creation (ContextVar — thread-safe)
            _thread_id_ctx.set(thread_id)
            _workspace_ctx.set(workspace)
            logger.debug("Sandbox deferred (lazy_init) for thread=%s", thread_id)
            return {"sandbox": None}
        else:
            return await self._acquire_sandbox(thread_id, workspace)

    # ------------------------------------------------------------------
    # awrap_tool_call — lazy creation on first sandboxed tool
    # ------------------------------------------------------------------

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Lazily create sandbox when a wrapped tool is first invoked."""
        tool_name = ""
        if hasattr(request, "tool_call") and hasattr(request.tool_call, "name"):
            tool_name = request.tool_call.get("name", "")
        elif hasattr(request, "tool") and isinstance(request.tool, dict):
            tool_name = request.tool.get("name", "")

        if tool_name not in SANDBOX_WRAPPED_TOOLS:
            return await handler(request)

        # Lazy creation on first sandboxed tool call
        tid = _thread_id_ctx.get()
        ws = _workspace_ctx.get()
        if self._lazy_init and tid:
            service = self._get_service()
            # Check if already created
            if tid not in service._pool:
                result = await self._acquire_sandbox(tid, ws)
                # Update tool context so the executor can find the sandbox
                if result and result.get("sandbox"):
                    try:
                        from harness.tools.code import set_tool_context
                        set_tool_context(
                            thread_id=tid,
                            sandbox=result["sandbox"],
                            workspace=ws,
                        )
                    except ImportError:
                        pass

        return await handler(request)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _acquire_sandbox(self, thread_id: str, workspace: str) -> dict | None:
        """Create the Docker sandbox container. Returns state update dict."""
        service = self._get_service()
        try:
            sandbox = await service.get_or_create(thread_id, workspace)
            if sandbox is not None:
                logger.debug("Sandbox acquired for thread=%s", thread_id)
            return {"sandbox": sandbox}
        except Exception as exc:
            logger.warning("Sandbox not available for thread=%s: %s", thread_id, exc)
            return {"sandbox": None}
