"""SandboxAuditMiddleware — audit sandbox shell/file operations for security logging.

Matches DeerFlow's design: wraps tool calls (awrap_tool_call) to classify
and log bash commands by risk level before execution.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import override

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)

# ── Risk classification patterns ──────────────────────────────────────────

_HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-[^\s]*r[^\s]*\s+(/\*?|~/?\*?|/home\b|/root\b)\s*$"),
    re.compile(r"dd\s+if="),
    re.compile(r"mkfs"),
    re.compile(r">+\s*/etc/"),
    re.compile(r"\|\s*(ba)?sh\b"),
    re.compile(r"[`$]\(?\s*(curl|wget|bash|sh|python|ruby|perl|base64)"),
    re.compile(r">+\s*(/usr/bin/|/bin/|/sbin/)"),
    re.compile(r">+\s*~/?\.(bashrc|profile|zshrc|bash_profile)"),
    re.compile(r"\b(LD_PRELOAD|LD_LIBRARY_PATH)\s*="),
    re.compile(r"/dev/tcp/"),
]

_MEDIUM_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"chmod\s+777"),
    re.compile(r"pip3?\s+install"),
    re.compile(r"apt(-get)?\s+install"),
    re.compile(r"\b(sudo|su)\b"),
    re.compile(r"\bPATH\s*="),
]

AUDITABLE_TOOLS = {"bash", "file_write", "str_replace", "write_file"}


class SandboxAuditMiddleware(HarnessAgentMiddleware):
    """Audit sandbox tool calls, logging high/medium risk commands.

    Uses ``awrap_tool_call`` to inspect each tool invocation. High-risk
    commands are logged at ERROR level; medium-risk at WARNING.
    """

    name = "sandbox_audit"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(command: str) -> str:
        for pattern in _HIGH_RISK_PATTERNS:
            if pattern.search(command):
                return "high"
        for pattern in _MEDIUM_RISK_PATTERNS:
            if pattern.search(command):
                return "medium"
        return "low"

    # ------------------------------------------------------------------
    # hook
    # ------------------------------------------------------------------

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = str(request.tool_call.get("name", ""))

        if tool_name in AUDITABLE_TOOLS:
            args = request.tool_call.get("args", {})
            command = args.get("command") or args.get("cmd") or args.get("content") or ""
            if isinstance(command, str) and command:
                risk = self._classify(command)
                if risk == "high":
                    logger.error(
                        "Sandbox audit HIGH risk: tool=%s cmd=%.200s",
                        tool_name, command,
                    )
                elif risk == "medium":
                    logger.warning(
                        "Sandbox audit MEDIUM risk: tool=%s cmd=%.200s",
                        tool_name, command,
                    )

                # Record audit event for all sandbox tool calls
                audit_action = f"{risk}_risk_command"
                try:
                    rt = getattr(request, "runtime", None)
                    if rt:
                        from harness.runtime.middleware_audit import audit
                        audit(rt, self.name, "awrap_tool_call", audit_action,
                              changes={"tool": tool_name, "risk": risk, "cmd": command[:200]})
                except Exception:
                    pass

        return await handler(request)
