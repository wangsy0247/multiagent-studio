"""TitleMiddleware — auto-generate a session title after the first reply.

The generated title is written directly into ``HarnessState.suggested_title``
and guarded by ``title_generated`` so it is only generated once per thread.
Because the title is part of the checkpointed state, behaviour is deterministic
across restarts.
"""
from __future__ import annotations

import logging
from typing import override

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_TITLE_PROMPT = "基于以下对话，生成一个简短的会话标题（5-15字）：\n\n{context}"


class TitleMiddleware(HarnessAgentMiddleware):
    """Generate a short title after the first assistant message.

    The title is stored in ``HarnessState.suggested_title`` and the guard flag
    ``title_generated`` prevents duplicate generation. Both fields are persisted
    by the LangGraph checkpointer.
    """

    name = "title"

    @override
    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        if state.get("title_generated"):
            return None

        messages = state.get("messages", [])
        has_ai = any(isinstance(m, AIMessage) for m in messages)
        if not has_ai:
            return None

        try:
            title = await self._generate_title(messages)
        except Exception as exc:
            logger.warning("Title generation failed: %s", exc)
            title = None

        return {
            "suggested_title": title or "新会话",
            "title_generated": True,
        }

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
            temperature=0.2,
            max_tokens=1024,
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )
        prompt = _TITLE_PROMPT.format(context="\n".join(context_lines))
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        title = str(response.content).strip()
        return title or "新会话"
