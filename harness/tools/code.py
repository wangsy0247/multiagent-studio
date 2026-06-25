"""Code execution tools with optional sandbox integration."""
from __future__ import annotations

import contextvars
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from harness.services.sandbox import SandboxService

logger = logging.getLogger(__name__)

_tool_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "harness_code_tool_ctx", default={}
)


def set_tool_context(
    thread_id: str | None = None,
    sandbox: SandboxService | None = None,
    workspace: str | None = None,
) -> None:
    """Set runtime context for code tools.

    Middleware wrappers can call this before invoking a tool so the tool can
    reach the per-thread sandbox without leaking those objects into the schema
    exposed to the LLM.
    """
    _tool_ctx.set({"thread_id": thread_id, "sandbox": sandbox, "workspace": workspace})


def _current_ctx() -> dict[str, Any]:
    return _tool_ctx.get()


def _sandbox_run(
    sandbox: SandboxService | None,
    thread_id: str | None,
    workspace: str,
    command: str | list[str],
    timeout: int,
) -> str | None:
    """Return the sandbox output when a context is available, otherwise None."""
    if sandbox is None or not thread_id:
        return None
    try:
        # get_or_create is async; this helper is only called inside async tools.
        import asyncio

        loop = asyncio.get_event_loop()
        future = sandbox.get_or_create(thread_id, workspace)
        if hasattr(future, "__await__"):
            loop.run_until_complete(future)
        return None
    except Exception as exc:
        logger.warning("Sandbox preparation failed: %s", exc)
        return None


def create_python_tool(sandbox: SandboxService | None = None) -> BaseTool:
    """Create the ``python`` tool backed by an optional sandbox."""

    @tool
    async def python(code: str, timeout: int = 30) -> str:
        """Execute Python code in an isolated sandbox.

        Args:
            code: Python source code to execute.
            timeout: Maximum execution time in seconds.
        """
        ctx = _current_ctx()
        sb = ctx.get("sandbox") or sandbox
        thread_id = ctx.get("thread_id")
        workspace = ctx.get("workspace", ".")

        if sb is not None and thread_id:
            try:
                await sb.get_or_create(thread_id, workspace)
                # ── 修复 #5: 写临时 .py 文件执行，避免 shell 注入 ──
                import tempfile, os as _os
                fd, tmp_path = tempfile.mkstemp(suffix=".py", dir=workspace)
                try:
                    with _os.fdopen(fd, "w") as f:
                        f.write(code)
                    return await sb.execute(
                        thread_id, f"python3 {tmp_path}", timeout=timeout
                    )
                finally:
                    try:
                        _os.unlink(tmp_path)
                    except OSError:
                        pass
            except Exception as exc:
                logger.warning("Sandbox python execution failed: %s", exc)
                return f"[error] sandbox execution failed: {exc}"

        return (
            "[mock python output]\n"
            f"Code received ({len(code)} chars). Sandbox not configured."
        )

    return python


def create_bash_tool(sandbox: SandboxService | None = None) -> BaseTool:
    """Create the ``bash`` tool backed by an optional sandbox."""

    @tool
    async def bash(command: str, timeout: int = 30) -> str:
        """Execute a shell command in an isolated sandbox.

        Args:
            command: Shell command to execute.
            timeout: Maximum execution time in seconds.
        """
        ctx = _current_ctx()
        sb = ctx.get("sandbox") or sandbox
        thread_id = ctx.get("thread_id")
        workspace = ctx.get("workspace", ".")

        if sb is not None and thread_id:
            try:
                await sb.get_or_create(thread_id, workspace)
                return await sb.execute(thread_id, command, timeout=timeout)
            except Exception as exc:
                logger.warning("Sandbox bash execution failed: %s", exc)
                return f"[error] sandbox execution failed: {exc}"

        return (
            "[mock bash output]\n"
            f"Command received: {command}. Sandbox not configured."
        )

    return bash


def create_execute_code_tool(sandbox: SandboxService | None = None) -> BaseTool:
    """Create the ``execute_code`` tool backed by an optional sandbox."""

    @tool
    async def execute_code(language: str, code: str, timeout: int = 30) -> str:
        """Execute code in a supported language inside the sandbox.

        Args:
            language: Programming language (python, bash, etc.).
            code: Source code to execute.
            timeout: Maximum execution time in seconds.
        """
        if language.lower() in ("python", "py", "python3"):
            python_tool = create_python_tool(sandbox)
            return await python_tool.ainvoke({"code": code, "timeout": timeout})

        ctx = _current_ctx()
        sb = ctx.get("sandbox") or sandbox
        thread_id = ctx.get("thread_id")
        workspace = ctx.get("workspace", ".")

        if sb is not None and thread_id:
            try:
                await sb.get_or_create(thread_id, workspace)
                return await sb.execute(thread_id, code, timeout=timeout)
            except Exception as exc:
                return f"[error] sandbox execution failed: {exc}"

        return f"[mock] {language} code not executed (no sandbox)"

    return execute_code


class CodeTools:
    """Container that injects a sandbox service into code tools."""

    def __init__(self, sandbox: SandboxService | None = None):
        self.sandbox = sandbox

    def python_tool(self) -> BaseTool:
        return create_python_tool(self.sandbox)

    def bash_tool(self) -> BaseTool:
        return create_bash_tool(self.sandbox)

    def execute_code_tool(self) -> BaseTool:
        return create_execute_code_tool(self.sandbox)

    def get_tools(self) -> list[BaseTool]:
        return [self.python_tool(), self.bash_tool(), self.execute_code_tool()]


def build_code_tools(sandbox: SandboxService | None = None) -> list[BaseTool]:
    """Return code execution tools optionally backed by a sandbox service."""
    return CodeTools(sandbox=sandbox).get_tools()


# Module-level convenience instances without a sandbox (mock output only).
_code_tools = CodeTools(sandbox=None)
python = _code_tools.python_tool()
bash = _code_tools.bash_tool()
execute_code = _code_tools.execute_code_tool()
