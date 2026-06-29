"""SubagentLimitMiddleware — cap the number of task calls per assistant turn.

Matches DeerFlow's design: runs at ``aafter_model``, inspects the last
AIMessage's tool_calls immediately after the model emits them, and truncates
any ``task`` calls beyond ``max_concurrent``.  Because ``create_agent``
executes all tool calls in a single batch, per-turn limiting is equivalent
to limiting concurrent SubAgents.
"""

from __future__ import annotations

import logging
from typing import override

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class SubagentLimitMiddleware(HarnessAgentMiddleware):
    """Prevent the Lead Agent from spawning more SubAgents than allowed per turn.

    Truncates excess ``task`` tool calls from the last AIMessage immediately
    after the model emits them (``aafter_model``), before the tools node
    dispatches them.
    """

    name = "subagent_limit"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        raw = self.config.get("max_concurrent", 3)
        self._max_concurrent: int = min(max(int(raw), 2), 4)

    @override
    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        messages = list(state.get("messages", []))

        # Find the last AIMessage with tool_calls
        ai_idx = None
        ai_msg = None
        for i, m in enumerate(reversed(messages)):
            if isinstance(m, AIMessage) and m.tool_calls:
                ai_idx = len(messages) - 1 - i
                ai_msg = m
                break

        if ai_msg is None:
            return None

        task_calls = [tc for tc in ai_msg.tool_calls if tc.get("name") == "task"]
        if len(task_calls) <= self._max_concurrent:
            return None

        # Build a new AIMessage that keeps only the first max_concurrent task
        # calls plus any non-task calls.
        kept_tool_calls: list[dict] = []
        task_seen = 0
        blocked = 0
        for tc in ai_msg.tool_calls:
            if tc.get("name") == "task":
                if task_seen < self._max_concurrent:
                    kept_tool_calls.append(tc)
                    task_seen += 1
                else:
                    blocked += 1
            else:
                kept_tool_calls.append(tc)

        new_messages = list(messages)
        new_messages[ai_idx] = ai_msg.model_copy(update={"tool_calls": kept_tool_calls})

        # Inject warning as a hidden HumanMessage — placed after the
        # AIMessage so it doesn't break tool-call pairing.
        warning = HumanMessage(
            content=(
                f"[系统提示] 当前助手一次最多发起 {self._max_concurrent} 个子 Agent 任务，"
                f"已自动忽略超出的 {blocked} 个任务。"
                f"请等待这些任务完成后再创建新的子 Agent。"
            ),
            additional_kwargs={"hide_from_ui": True},
        )
        new_messages.append(warning)

        logger.info(
            "SubagentLimit blocked %d task call(s) for thread=%s",
            blocked, state.get("thread_id"),
        )
        # ── Audit ──
        from harness.runtime.middleware_audit import audit
        audit(runtime, self.name, "aafter_model", "truncated_task_calls",
              changes={"blocked": blocked, "kept": task_seen, "limit": self._max_concurrent})
        return {"messages": new_messages}
