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
from harness.middleware.dynamic_context import is_dynamic_context_reminder
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_TITLE_PROMPT = "基于以下对话，生成一个简短的会话标题（5-15字）：\n\n{context}"
_SUMMARY_MESSAGE_NAME = "summary"


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
            return None

        if not title:
            return None

        logger.info("Title generated: %s", title)
        return {
            "suggested_title": title,
            "title_generated": True,
        }

    async def _generate_title(self, messages: list) -> str:
        # 过滤掉动态 context reminder、summary 消息，只保留真实对话
        context_lines: list[str] = []
        for m in messages:
            if is_dynamic_context_reminder(m):
                continue
            if not isinstance(m, (HumanMessage, AIMessage)):
                continue
            if getattr(m, "name", None) == _SUMMARY_MESSAGE_NAME:
                continue
            role = "用户" if isinstance(m, HumanMessage) else "AI"
            content = str(m.content)[:120]
            if content.strip():
                context_lines.append(f"{role}: {content}")
            if len(context_lines) >= 6:
                break

        if not context_lines:
            return ""

        model_name = self.config.get("title_model", "qwen3.5-flash")
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "")
        logger.debug("Title LLM: model=%s api_key_set=%s base_url=%s",
                     model_name, bool(api_key), base_url)
        llm = ChatOpenAI(
            model=model_name,
            temperature=0.2,
            api_key=api_key,
            base_url=base_url,
        )
        prompt = _TITLE_PROMPT.format(context="\n".join(context_lines))
        logger.debug("Title prompt: %s", prompt[:200])
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        result = str(response.content).strip()
        logger.debug("Title response: %s", result[:100])
        return result
