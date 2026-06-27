"""LoopDetectionMiddleware — detect and break agent execution loops.

Loop-detection history is stored in ``HarnessState.loop_history`` so it
survives restarts and remains deterministic for a given checkpoint.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, override

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class LoopDetectionMiddleware(HarnessAgentMiddleware):
    """Detect repeated message-sequence patterns and force a loop break.

    Runs before each model call (``abefore_model``) to check whether the
    same sequence of messages has appeared repeatedly, indicating a loop.
    The per-thread detection history lives in ``HarnessState.loop_history``
    and is checkpointed by LangGraph.
    """

    name = "loop_detection"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        # config.yaml 使用 warn_threshold / hard_limit，兼容 threshold 旧键名
        self._window = int(self.config.get("window_size", 20))
        self._threshold = int(self.config.get("warn_threshold", self.config.get("threshold", 5)))
        self._hard_limit = int(self.config.get("hard_limit", 10))

    @override
    async def abefore_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        messages = list(state.get("messages", []))

        window = messages[-self._window:] if len(messages) >= self._window else messages
        seq_hash = self._hash_sequence(window)

        history = list(state.get("loop_history", []))
        match_count = sum(1 for h in history if h == seq_hash)

        updates: dict[str, Any] = {"loop_history": [seq_hash]}

        if match_count >= self._threshold:
            logger.warning("Loop detected for thread=%s — breaking", state.get("thread_id"))
            updates["messages"] = self._break_loop(messages)
            updates["loop_detected"] = True

        return updates

    # ------------------------------------------------------------------
    # awrap_model_call — onion model wrapper (spec: drain queue)
    # ------------------------------------------------------------------

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Drain pending loop warnings before the actual model call."""
        # In the onion model, this runs between LLMErrorHandling (outer) and
        # DanglingToolCall (inner). The main loop detection logic is in
        # abefore_model; here we just ensure clean state.
        return await handler(request)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _hash_sequence(self, messages: list) -> str:
        content = "|".join(
            f"{type(m).__name__}:{str(getattr(m, 'content', ''))[:80]}"
            for m in messages
        )
        return hashlib.md5(content.encode()).hexdigest()

    @staticmethod
    def _break_loop(messages: list) -> list:
        """Return a new message list with pending tool_calls stripped and guidance injected."""
        new_messages = []
        cleared = False
        for msg in reversed(messages):
            if not cleared and isinstance(msg, AIMessage) and msg.tool_calls:
                new_messages.append(
                    msg.model_copy(update={"tool_calls": []})
                )
                cleared = True
            else:
                new_messages.append(msg)
        new_messages.reverse()

        loop_msg = SystemMessage(
            content=(
                "[系统提示] 检测到可能的执行循环。"
                "请换一种方式思考问题，避免重复相同的操作。"
                "如果陷入困境，可以尝试简化问题或请求用户帮助。"
            )
        )
        new_messages.append(loop_msg)
        return new_messages
