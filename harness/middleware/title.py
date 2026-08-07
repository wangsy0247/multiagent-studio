"""TitleMiddleware — auto-generate a session title after the first reply.

The generated title is written into ``HarnessState.suggested_title`` and
guarded by ``title_generated``.  Both fields are checkpoint-persisted.

Ported from the reference implementation with the same single-hook design: ``aafter_model``
generates the title synchronously and returns it as a state update.  The
ChatOpenAI instance is cached (lazy-init) so connection pooling keeps
subsequent calls fast.

Improvements over the reference design:
  - precise trigger — only after the first complete exchange (1 user + ≥1 assistant)
  - ``_title_emitted_ref`` — cross-run dedup (not needed in the reference design because
    ``state.title`` is checked against a different field name)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import override

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware.dynamic_context import is_dynamic_context_reminder
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_TITLE_PROMPT = "基于以下对话，生成一个简短的会话标题（5-15字）：\n\n{context}"
_SUMMARY_MESSAGE_NAME = "summary"
_FALLBACK_MAX_CHARS = 20


def _strip_think_tags(text: str) -> str:
    """Remove ``<think>...</think>`` blocks emitted by reasoning models (DeepSeek-R1, minimax, etc.)."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()


def _normalize_content(content: object) -> str:
    """Recursively extract text from string / list / dict content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_normalize_content(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text_value = content.get("text")
        if isinstance(text_value, str):
            return text_value
        nested = content.get("content")
        if nested is not None:
            return _normalize_content(nested)
    return ""


class TitleMiddleware(HarnessAgentMiddleware):
    """Generate a short title after the first assistant message.

    Mirrors the standard ``TitleMiddleware``: a single ``aafter_model`` hook
    that generates the title synchronously and returns it as a state update.
    The ``ChatOpenAI`` instance is cached so connection pooling keeps
    subsequent calls fast.
    """

    name = "title"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # Cached ChatOpenAI instance — reuse across calls for connection pooling
        self._llm: ChatOpenAI | None = None
        self._llm_config_hash: int = 0

    # ------------------------------------------------------------------
    # hook (single hook — harness pattern)
    # ------------------------------------------------------------------

    @override
    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        # ── 已生成过 → 跳过 ──
        if state.get("title_generated"):
            return None

        # ── 共享去重标志 (跨多次 graph run) ──
        title_emitted_ref: list | None = self.config.get("_title_emitted_ref")
        if title_emitted_ref and title_emitted_ref[0]:
            return None

        messages = state.get("messages", [])

        # ── 精确触发: 恰好 1 条用户消息 + ≥1 条助手消息 (第一次完整交换后) ──
        user_msgs = [
            m for m in messages
            if isinstance(m, HumanMessage) and not is_dynamic_context_reminder(m)
        ]
        assistant_msgs = [m for m in messages if isinstance(m, AIMessage)]
        if len(user_msgs) != 1 or len(assistant_msgs) < 1:
            return None

        user_msg = _normalize_content(user_msgs[0].content)
        user_id = state.get("user_id", "default")

        # ── 生成标题 (LLM → 失败则 fallback) ──
        title = None
        try:
            title = await self._generate_title(messages, user_id=user_id)
        except Exception as exc:
            logger.warning("Title generation failed: %s", exc)
        if not title:
            title = self._fallback_title(user_msg)

        logger.info("Title generated: %s", title)

        # ── 标记已生成 ──
        if title_emitted_ref is not None:
            title_emitted_ref[0] = True

        # ── 回调: 推送标题到 SSE ──
        on_title = self.config.get("on_title")
        if on_title is not None:
            try:
                if asyncio.iscoroutinefunction(on_title):
                    await on_title(title)
                else:
                    on_title(title)
            except Exception as exc:
                logger.warning("on_title callback failed: %s", exc)

        return {
            "suggested_title": title,
            "title_generated": True,
        }

    # ------------------------------------------------------------------
    # title generation (harness pattern)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_user_message_for_title(message: object) -> bool:
        """harness-compatible user message filter."""
        return (
            isinstance(message, HumanMessage)
            and not is_dynamic_context_reminder(message)
        )

    @staticmethod
    def _fallback_title(user_msg: str) -> str:
        """Truncate the first user message as a fallback title (harness pattern)."""
        cleaned = user_msg.strip()
        if not cleaned:
            return "新会话"
        if len(cleaned) > _FALLBACK_MAX_CHARS:
            return cleaned[:_FALLBACK_MAX_CHARS].rstrip() + "..."
        return cleaned

    def _build_title_prompt(self, messages: list) -> tuple[str, str]:
        """Build prompt from first user + last assistant message (harness pattern).

        Returns (prompt, user_msg) so the caller can use user_msg as fallback.
        """
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        for m in messages:
            if self._is_user_message_for_title(m):
                content = _normalize_content(m.content)
                if content.strip():
                    user_msgs.append(content)
            elif isinstance(m, AIMessage):
                if getattr(m, "name", None) == _SUMMARY_MESSAGE_NAME:
                    continue
                content = _normalize_content(m.content)
                content = _strip_think_tags(content)
                if content.strip():
                    assistant_msgs.append(content)

        user_msg = user_msgs[0][:500] if user_msgs else ""
        assistant_msg = assistant_msgs[-1][:500] if assistant_msgs else ""

        prompt = _TITLE_PROMPT.format(
            context=f"用户: {user_msg}\nAI: {assistant_msg}"
        )
        return prompt, user_msg

    async def _generate_title(self, messages: list, user_id: str = "default") -> str:
        """Generate title via LLM (harness pattern — single LLM call)."""
        prompt, _ = self._build_title_prompt(messages)
        if not prompt:
            return ""

        model_name = self.config.get("title_model", "") or "gpt-4o-mini"
        api_key, base_url = self._resolve_credentials(user_id)

        import time as _time
        t0 = _time.monotonic()
        llm = self._get_llm(api_key, base_url, model_name)
        t1 = _time.monotonic()
        logger.info(
            "Title LLM: model=%s _get_llm=%.2fs (cached=%s)",
            model_name, t1 - t0, self._llm_config_hash != 0,
        )

        logger.debug("Title prompt: %s", prompt[:200])
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        t2 = _time.monotonic()
        logger.info("Title ainvoke took %.2fs (total %.2fs)", t2 - t1, t2 - t0)

        result = _strip_think_tags(str(response.content)).strip().strip('"').strip("'")
        logger.debug("Title response: %s", result[:100])
        return result

    # ------------------------------------------------------------------
    # LLM instance cache
    # ------------------------------------------------------------------

    def _get_llm(self, api_key: str, base_url: str, model_name: str) -> ChatOpenAI:
        """Return a cached ``ChatOpenAI``, recreated only when credentials change.

        Reusing the same instance keeps the httpx connection pool warm —
        subsequent title calls skip the TCP + TLS handshake.

        Configuration matches the main agent's ``_init_llm`` as closely as
        possible to avoid divergent API behaviour between the two code paths.
        """
        config_hash = hash((api_key, base_url, model_name))
        if self._llm is None or self._llm_config_hash != config_hash:
            self._llm = ChatOpenAI(
                model=model_name,
                temperature=0.2,
                api_key=api_key,
                base_url=base_url,
                request_timeout=30,
                max_retries=1,
                extra_body={
                    "enable_thinking": False,          # DashScope / 通义千问
                    "thinking": {"type": "disabled"},   # DeepSeek / Anthropic / Claude
                },
            )
            self._llm_config_hash = config_hash
        return self._llm

    def _resolve_credentials(self, user_id: str) -> tuple[str, str]:
        """Resolve api_key / base_url (EffectiveConfig 服务器注入 → env)."""
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "")
        api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        return api_key, base_url
