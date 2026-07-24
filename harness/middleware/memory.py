"""MemoryMiddleware — queue conversation for memory update after agent execution.

Adapted from DeerFlow: only ``aafter_agent`` (no ``abefore_agent``).
Memory reading/injection is handled by DynamicContextMiddleware.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, override

from langgraph.config import get_config
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware.dynamic_context import is_dynamic_context_reminder
from harness.memory.message_processing import (
    detect_correction,
    detect_reinforcement,
    filter_messages_for_memory,
)
from harness.memory.queue import get_memory_queue
from harness.config.memory_config import get_memory_config
from harness.models import HarnessState

if TYPE_CHECKING:
    from harness.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryMiddleware(HarnessAgentMiddleware):
    """Middleware that queues conversation for memory update after agent execution.

    This middleware:
    1. After each agent execution, queues the latest exchange for memory update
    2. Only submits the final assistant reply + the user message preceding it —
       since ``aafter_agent`` fires once per turn, this pair is inherently
       incremental (no cursor / full-history replay needed). The leading hidden
       dynamic-context reminder (date/memory injection) is included to give the
       fact extractor temporal and existing-memory context.
    3. The queue uses debouncing to batch multiple updates together
    4. Memory is updated asynchronously via LLM summarization (file) or mem0.add()
    """

    name = "memory"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None,
                 memory_config: "MemoryConfig | None" = None):
        super().__init__(config)
        self._agent_name = agent_name
        self._memory_config = memory_config

    @override
    async def aafter_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        """Queue the latest exchange for memory update after agent completes."""
        # Per-user enabled 优先, 回退到全局 MemoryConfig
        per_user_enabled = self.config.get("memory_enabled", None)
        if per_user_enabled is False:
            return None
        if per_user_enabled is None:
            mem_cfg = self._memory_config or get_memory_config()
            if not mem_cfg.enabled:
                return None

        # Get thread_id from runtime context or LangGraph config
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            config_data = get_config()
            thread_id = config_data.get("configurable", {}).get("thread_id")
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        user_id = state.get("user_id", "")

        # 过滤出用户输入与最终回复 (剥离 tool 往返 / 上传块 / 摘要消息)
        filtered = filter_messages_for_memory(messages)

        # 当前轮次的最终回复 = 最后一条无 tool_calls 的 AI 消息
        last_ai_idx = next(
            (i for i in range(len(filtered) - 1, -1, -1)
             if getattr(filtered[i], "type", None) == "ai"),
            None,
        )
        if last_ai_idx is None:
            return None
        last_ai = filtered[last_ai_idx]

        # 被截断 (length / content_filter 等) 的回复不提炼进记忆;
        # provider 未上报 finish_reason 时 (None) 视为正常完成.
        finish_reason = (
            getattr(last_ai, "response_metadata", None) or {}
        ).get("finish_reason")
        if finish_reason is not None and finish_reason != "stop":
            logger.debug(
                "Skip memory update (thread=%s): finish_reason=%s",
                thread_id, finish_reason,
            )
            return None

        # 与最终回复配对的用户消息 = 它之前最近的一条 human
        last_human = next(
            (m for m in reversed(filtered[:last_ai_idx])
             if getattr(m, "type", None) == "human"),
            None,
        )
        if last_human is None:
            return None

        # 第一条隐藏的动态上下文提醒 (日期/记忆注入) 一并提交,
        # 为事实提取提供时间与已有记忆上下文; 该消息在压缩时由
        # SummarizationMiddleware._preserve_dynamic_context_reminders 保留.
        leading: list = []
        if (
            filtered
            and is_dynamic_context_reminder(filtered[0])
            and filtered[0] is not last_human
        ):
            leading.append(filtered[0])

        latest_exchange = [*leading, last_human, last_ai]

        correction_detected = detect_correction(latest_exchange)
        reinforcement_detected = not correction_detected and detect_reinforcement(latest_exchange)

        # 时间 metadata（mem0 backend 用）
        current_time_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        metadata = {"event_time": current_time_iso, "thread_id": thread_id}

        queue = get_memory_queue()
        queue.add(
            thread_id=thread_id,
            messages=latest_exchange,
            agent_name=self._agent_name,
            user_id=user_id,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            metadata=metadata,
            api_key=self.config.get("openai_api_key", ""),
            base_url=self.config.get("openai_base_url", ""),
            model_name=self.config.get("memory_model", ""),
            enabled=self.config.get("memory_enabled", None),
        )

        return None
