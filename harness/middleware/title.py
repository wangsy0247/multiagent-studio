"""TitleMiddleware — auto-generate a session title after the first reply.

Uses ``asyncio.create_task`` to generate titles in the background so they
never block the main ReAct loop / SSE stream.
"""
from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_TITLE_PROMPT = "基于以下对话，生成一个简短的会话标题（5-15字）：\n\n{context}"


class TitleMiddleware(HarnessAgentMiddleware):
    """Generate a short title after the first assistant message.

    Title generation is *fire-and-forget* — ``aafter_model`` launches a
    background ``asyncio.Task`` and returns immediately.  The caller polls
    ``get_pending_title()`` to pick up the result without blocking.
    """

    name = "title"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._generated: set[str] = set()
        self._pending_titles: dict[str, str | None] = {}  # thread_id → title

    # ------------------------------------------------------------------
    # aafter_model — non-blocking fire-and-forget
    # ------------------------------------------------------------------

    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        thread_id = state.get("thread_id", "")
        if not thread_id or thread_id in self._generated:
            return None

        messages = state.get("messages", [])
        has_ai = any(isinstance(m, AIMessage) for m in messages)
        if not has_ai:
            return None

        # 标记已触发（防止重复启动后台任务）
        self._generated.add(thread_id)
        # 启动后台任务，不阻塞 ReAct 循环
        asyncio.create_task(self._generate_and_store(thread_id, messages))
        return None  # 不阻塞 — 调用方通过 get_pending_title() 轮询结果

    async def _generate_and_store(self, thread_id: str, messages: list) -> None:
        try:
            title = await self._generate_title(messages)
            self._pending_titles[thread_id] = title
            logger.info("Title generated (async) for thread=%s: %s", thread_id, title)
        except Exception as exc:
            logger.warning("Title generation failed: %s", exc)
            self._pending_titles[thread_id] = None

    def get_pending_title(self, thread_id: str) -> str | None:
        """Non-blocking — return completed background title or None."""
        return self._pending_titles.pop(thread_id, None)

    # ------------------------------------------------------------------
    # actual LLM call
    # ------------------------------------------------------------------

    async def _generate_title(self, messages: list) -> str:
        context_lines: list[str] = []
        for m in messages[:6]:
            role = "用户" if isinstance(m, HumanMessage) else "AI"
            content = str(m.content)[:120]
            if content.strip():
                context_lines.append(f"{role}: {content}")

        if not context_lines:
            return "新会话"

        model_name = self.config.get("title_model", "gpt-4o-mini")
        llm = ChatOpenAI(
            model=model_name,
            temperature=0.3,
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )
        prompt = _TITLE_PROMPT.format(context="\n".join(context_lines))
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        title = str(response.content).strip()
        return title or "新会话"
