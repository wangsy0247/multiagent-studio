"""memory_search tool — let Agent proactively query mem0 long-term memory.

mem0 stores facts/preferences extracted from past conversations. This tool
provides on-demand precise retrieval, complementing DynamicContextMiddleware's
passive injection (which uses memory.json for general context).

Architecture:
    memory.json (passive injection)  ←  DynamicContextMiddleware (first turn only)
    mem0 (active query)              ←  memory_search tool (on-demand)

Both are written to by MemoryUpdater when mem0_tool_enabled=true (dual-write).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)


def create_memory_search_tool() -> BaseTool:
    """Create the ``memory_search`` tool for querying mem0.

    The tool reads user_id and agent_id from LangGraph runtime context
    automatically — the LLM only needs to provide the query string.

    Returns:
        A BaseTool instance that can be added to an agent's tool list.
    """

    @tool
    async def memory_search(query: str) -> str:
        """Search the user's long-term memory for relevant facts and preferences.

        This tool accesses memories extracted from ALL past conversations
        (not just the current one). Use it when you need information the user
        may have shared before but isn't in the current conversation.

        —— WHEN TO USE ——
        - User references past info: "continue last time", "like before",
          "do you remember...", "that project I mentioned"
        - Need personalization: "recommend a movie" — check preferences first
        - Missing context: user's request lacks info they likely shared before
        - User asks about their own history: "what did I tell you about X"

        —— WHEN NOT TO USE ——
        - Current conversation already has all needed information
        - Brand new topic with no connection to past conversations
        - User uploaded a file or gave complete specs (current context suffices)
        - Pure factual Q&A (answer doesn't depend on who the user is)

        —— GUIDELINES ——
        - When unsure whether to search: search once (it's cheap, ~50ms)
        - If results are empty, don't repeat the same query
        - Frame queries naturally: "user's Python preferences" not just "Python"
        - Results show facts/preferences, not raw conversation transcripts

        Args:
            query: Natural language describing what you're looking for.
                   Good queries specify the topic: "user's preferred programming
                   language" or "project names user mentioned".
        """
        from harness.memory.mem0_client import get_mem0
        from langgraph.config import get_config

        # ── 1. 获取 mem0 实例 ──
        mem0 = get_mem0()
        if mem0 is None:
            return ("Memory search unavailable: mem0 not initialized. "
                    "Proceed with information available in the current context.")

        # ── 2. 从 LangGraph 配置自动获取 user_id 和 agent_id ──
        # LLM 不需要（也不应该）手动传这些参数
        try:
            config = get_config()
        except RuntimeError:
            # 在非 LangGraph 上下文中调用（如测试）
            config = {}

        configurable = config.get("configurable", {}) or {}
        user_id = configurable.get("user_id", "default")
        agent_id = configurable.get("agent_name") or "lead_agent"

        # 构建 filters
        filters: dict[str, Any] = {"user_id": user_id}
        if agent_id:
            filters["agent_id"] = agent_id

        # ── 3. 调用 mem0.search()（线程池避免阻塞事件循环）──
        try:
            results = await asyncio.to_thread(
                mem0.search,
                query=query,
                filters=filters,
                top_k=5,
            )
        except Exception as e:
            logger.warning("memory_search failed (query=%r, user=%s): %s",
                          query, user_id, e)
            return f"Memory search encountered an error: {e}. Please proceed with current context."

        # ── 4. 解析结果 ──
        # mem0 返回格式可能是：
        #   {"results": [{"id":..., "memory":..., "score":...}]}  (标准)
        #   [{"id":..., "memory":...}]                           (某些版本)
        #   {"memories": [...]}                                   (旧版)
        if isinstance(results, dict):
            items = results.get("results") or results.get("memories") or []
        elif isinstance(results, list):
            items = results
        else:
            items = []

        # ── 5. 空结果处理 ──
        if not items:
            return "No relevant memories found. This may be a new topic or the user hasn't shared related information before."

        # ── 6. 格式化输出 ──
        lines = [f"Found {len(items)} relevant memories:"]
        for i, item in enumerate(items, 1):
            # mem0 的记忆内容字段可能是 "memory" 或 "content"
            content = item.get("memory", "") or item.get("content", "")
            if not content:
                continue
            # 附带 score（如果有）方便 LLM 判断可信度
            score = item.get("score")
            if score is not None:
                lines.append(f"{i}. {content} (relevance: {score:.2f})")
            else:
                lines.append(f"{i}. {content}")

        result_text = "\n".join(lines)
        logger.info(
            "memory_search: query=%r user=%s → %d results",
            query[:50], user_id, len(items),
        )
        return result_text

    return memory_search
