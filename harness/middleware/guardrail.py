"""GuardrailMiddleware — tool-calling permission enforcement."""
from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class GuardrailMiddleware(HarnessAgentMiddleware):
    """Filter the tool list according to agent-type permissions.

    Supports allow-list and deny-list per agent type with a configurable
    default policy (``allow`` or ``deny``).  Stores guardrail metadata on
    ``state["metadata"]`` for downstream enforcement.
    """

    name = "guardrail"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        agent_type = state.get("agent_type", "default")
        permissions: dict = self.config.get("tool_permissions", {})
        default_policy = self.config.get("default_policy", "allow")

        perms = permissions.get(agent_type, {})
        deny_list: list[str] = list(perms.get("deny_list", []))
        allow_list: list[str] | None = perms.get("allow_list")

        metadata = dict(state.get("metadata", {}))
        metadata["_guardrail_deny_list"] = deny_list
        if allow_list is not None:
            metadata["_guardrail_allow_list"] = allow_list

        return {"metadata": metadata}

    @staticmethod
    def check_permission(
        tool_name: str,
        deny_list: list[str],
        allow_list: list[str] | None,
        default_policy: str,
    ) -> bool:
        """Return True if the tool is allowed."""
        if tool_name in deny_list:
            return False
        if allow_list is not None and tool_name not in allow_list:
            return False
        return default_policy == "allow"
