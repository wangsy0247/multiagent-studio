"""LLMErrorHandlingMiddleware — wrap model calls with retry.

Matches DeerFlow spec: wrap_model_call stage, onion-model nested.
Retries failed LLM calls with exponential backoff, then falls back to
an error message injected into the conversation.

This implementation is intentionally stateless: it does not keep a per-thread
circuit-breaker counter in instance memory, so behaviour is fully determined by
the LangGraph checkpoint.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, override

from langchain_core.messages import AIMessage

from harness.middleware.base import HarnessAgentMiddleware

logger = logging.getLogger(__name__)

# Retryable HTTP status codes
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMErrorHandlingMiddleware(HarnessAgentMiddleware):
    """Wrap LLM calls with retry (exponential backoff) and graceful fallback.

    Onion position: outermost wrapper in wrap_model_call (registered last among
    model-call wrappers, so it wraps everything else).

    Parameters
    ----------
    max_retries : int
        Maximum retry attempts (default 3).
    base_delay : float
        Initial backoff delay in seconds (default 1.0).
    """

    name = "llm_error_handling"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.max_retries: int = self.config.get("max_retries", 3)
        self.base_delay: float = self.config.get("base_delay", 1.0)

    # ------------------------------------------------------------------
    # awrap_model_call — the core hook
    # ------------------------------------------------------------------

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Wrap the model call with retry + error handling.

        On failure after all retries, inject an error AIMessage instead of
        crashing the entire agent run.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries and self._is_retryable(exc):
                    delay = self.base_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self.max_retries + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    break

        # All retries exhausted — graceful fallback
        logger.error(
            "LLM call failed after %d retries: %s",
            self.max_retries + 1, last_error,
        )

        # Return a synthetic error AIMessage so the agent can recover
        error_msg = f"抱歉，模型调用暂时失败（已重试 {self.max_retries} 次）。请稍后重试或检查 API 配置。"
        if last_error:
            error_msg += f" 错误详情: {str(last_error)[:200]}"

        return AIMessage(content=error_msg, additional_kwargs={"llm_error": True})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_retryable(self, exc: Exception) -> bool:
        """Check if an exception is retryable."""
        # Check for HTTP status codes in the exception chain
        msg = str(exc).lower()
        for code in RETRYABLE_STATUS:
            if str(code) in msg:
                return True
        # Common retryable patterns
        retryable_keywords = [
            "timeout", "timed out", "rate limit", "too many requests",
            "service unavailable", "server error", "internal server error",
            "connection", "network", "reset by peer", "broken pipe",
        ]
        return any(kw in msg for kw in retryable_keywords)
