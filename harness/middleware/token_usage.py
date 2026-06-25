"""TokenUsageMiddleware — track token consumption per LLM call.

Matches DeerFlow ``TokenUsageMiddleware``: after each model call, extract usage
metadata from the AIMessage response and accumulate into state.token_usage.
"""
from __future__ import annotations

import logging

from langgraph.runtime import Runtime
from langchain_core.messages import AIMessage

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState, TokenUsage

logger = logging.getLogger(__name__)


class TokenUsageMiddleware(HarnessAgentMiddleware):
    """Track token usage after each model call and accumulate into state."""

    name = "token_usage"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.enabled = True

    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Extract token usage from the latest AIMessage and accumulate."""
        if not self.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        # Extract usage from LangChain response_metadata
        usage_meta = getattr(last_msg, "usage_metadata", None) or {}
        response_meta = getattr(last_msg, "response_metadata", {}) or {}

        input_tokens = usage_meta.get("input_tokens", 0)
        output_tokens = usage_meta.get("output_tokens", 0)
        total_tokens = usage_meta.get("total_tokens", 0)

        # Fallback to token_usage in additional_kwargs (OpenAI format)
        if total_tokens == 0:
            token_info = last_msg.additional_kwargs.get("token_usage", {}) if hasattr(last_msg, "additional_kwargs") else {}
            input_tokens = token_info.get("prompt_tokens", 0)
            output_tokens = token_info.get("completion_tokens", 0)
            total_tokens = token_info.get("total_tokens", 0)

        if total_tokens == 0:
            return None

        # Accumulate
        current = state.get("token_usage", TokenUsage())
        updated = TokenUsage(
            prompt_tokens=current.prompt_tokens + input_tokens,
            completion_tokens=current.completion_tokens + output_tokens,
            total_tokens=current.total_tokens + total_tokens,
            cost_usd=current.cost_usd,  # cost calculation handled by ObservabilityManager
        )

        logger.debug(
            "Token usage: +%d input, +%d output, total=%d",
            input_tokens, output_tokens, updated.total_tokens,
        )
        return {"token_usage": updated}
