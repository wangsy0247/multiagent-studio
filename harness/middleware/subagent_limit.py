"""SubagentLimitMiddleware — cap the number of concurrently running SubAgents."""
from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class SubagentLimitMiddleware(HarnessAgentMiddleware):
    """Prevent the Lead Agent from spawning more SubAgents than allowed.

    The maximum concurrent count is clamped to [2, 4].
    Uses ``awrap_tool_call`` to track active subagent count around ``task``
    tool executions, and ``abefore_model`` to strip excess ``task`` tool
    calls when the limit is reached.
    """

    name = "subagent_limit"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        raw = self.config.get("max_concurrent", 3)
        self._max_concurrent: int = min(max(int(raw), 2), 4)
        self._active: dict[str, int] = {}

    # -- Track active count around task tool executions --

    async def awrap_tool_call(self, request, handler):
        """Increment/decrement active count around task tool calls."""
        tool_name = request.tool_call.get("name", "")
        if tool_name != "task":
            return await handler(request)

        thread_id = request.state.get("thread_id", "default")
        self._active[thread_id] = self._active.get(thread_id, 0) + 1
        try:
            result = await handler(request)
            return result
        finally:
            self._active[thread_id] = max(0, self._active.get(thread_id, 0) - 1)

    # -- Strip excess task calls before model sees them --

    async def abefore_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        thread_id = state.get("thread_id", "default")
        current = self._active.get(thread_id, 0)
        if current < self._max_concurrent:
            return None

        messages = list(state.get("messages", []))
        # Find the last AIMessage with tool_calls
        ai_msg = None
        for m in reversed(messages):
            if isinstance(m, AIMessage) and m.tool_calls:
                ai_msg = m
                break

        if ai_msg is None:
            return None

        filtered: list[dict] = []
        blocked = 0
        for tc in ai_msg.tool_calls:
            if tc["name"] == "task" and current >= self._max_concurrent:
                blocked += 1
            else:
                filtered.append(tc)

        if blocked == 0:
            return None

        ai_msg.tool_calls = filtered
        warning = SystemMessage(
            content=(
                f"[系统提示] 当前已有 {current} 个子 Agent 在运行，"
                f"达到最大并发限制 ({self._max_concurrent})。"
                f"请等待部分任务完成后再创建新的子 Agent。"
            )
        )
        messages.append(warning)
        logger.info("SubagentLimit blocked %d task call(s) for thread=%s", blocked, thread_id)
        return {"messages": messages}
