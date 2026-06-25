"""LoopDetectionMiddleware — detect and break agent execution loops."""
from __future__ import annotations

import hashlib
import logging
from collections import deque
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class LoopDetectionMiddleware(HarnessAgentMiddleware):
    """Detect repeated message-sequence patterns and force a loop break.

    Runs before each model call (``abefore_model``) to check whether the
    same sequence of messages has appeared repeatedly, indicating a loop.
    """

    name = "loop_detection"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        # config.yaml 使用 warn_threshold / hard_limit，兼容 threshold 旧键名
        self._window = int(self.config.get("window_size", 20))
        self._threshold = int(self.config.get("warn_threshold", self.config.get("threshold", 5)))
        self._hard_limit = int(self.config.get("hard_limit", 10))
        self._histories: dict[str, deque] = {}

    def cleanup_thread(self, thread_id: str) -> None:
        """Remove per-thread state to prevent memory leaks (#9)."""
        self._histories.pop(thread_id, None)

    async def abefore_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        thread_id = state.get("thread_id", "default")
        messages = list(state.get("messages", []))

        window = messages[-self._window:] if len(messages) >= self._window else messages
        seq_hash = self._hash_sequence(window)

        if thread_id not in self._histories:
            self._histories[thread_id] = deque(maxlen=self._window * self._threshold)

        history = self._histories[thread_id]
        match_count = sum(1 for h in history if h == seq_hash)

        if match_count >= self._threshold:
            logger.warning("Loop detected for thread=%s — breaking", thread_id)
            messages = self._break_loop(messages)
            history.append(seq_hash)
            return {"messages": messages, "loop_detected": True}

        history.append(seq_hash)
        return None

    # ------------------------------------------------------------------
    # awrap_model_call — onion model wrapper (spec: drain queue)
    # ------------------------------------------------------------------

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
        """Strip pending tool_calls and inject guidance."""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                msg.tool_calls = []
                break

        loop_msg = SystemMessage(
            content=(
                "[系统提示] 检测到可能的执行循环。"
                "请换一种方式思考问题，避免重复相同的操作。"
                "如果陷入困境，可以尝试简化问题或请求用户帮助。"
            )
        )
        messages.append(loop_msg)
        return messages
