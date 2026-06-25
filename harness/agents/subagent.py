"""SubAgent — independent agent instance for delegated tasks.

Each SubAgent uses ``create_agent()`` internally for its ReAct loop,
with a subset of middlewares appropriate for isolated task execution.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState, SubAgentConfig, SubAgentResult

logger = logging.getLogger(__name__)


class SubAgent:
    """Independent agent with its own system prompt, tools, and model.

    Executes a single delegated task using ``create_agent()`` with an
    optional middleware list.  Each SubAgent gets its own compiled graph.
    """

    def __init__(
        self,
        config: SubAgentConfig,
        llm: BaseChatModel,
        tools: list[BaseTool],
        middlewares: list[HarnessAgentMiddleware] | None = None,
    ):
        self.config = config
        # Filter out disallowed tools
        disallowed = set(config.disallowed_tools or [])
        filtered_tools = [t for t in tools if t.name not in disallowed]

        # Build the sub-agent graph via create_agent
        self._graph = create_agent(
            model=llm,
            tools=filtered_tools,
            system_prompt=config.system_prompt,
            middleware=middlewares or [],
            state_schema=HarnessState,
        )
        logger.debug("SubAgent '%s' compiled with %d tools", config.name, len(filtered_tools))

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        instruction: str,
        context: str = "",
        parent_state: HarnessState | None = None,
    ) -> SubAgentResult:
        """Execute the delegated task using create_agent().

        Parameters
        ----------
        instruction : str
            The task description for this SubAgent.
        context : str
            Additional background information.
        parent_state : HarnessState | None
            The invoking Lead Agent's state for thread / user id inheritance.
        """
        messages: list[AnyMessage] = []

        if context:
            messages.append(SystemMessage(content=f"[上下文]\n{context}"))

        messages.append(HumanMessage(content=instruction))

        thread_id = (
            parent_state.get("thread_id", "")
            if parent_state
            else ""
        )
        user_id = (
            parent_state.get("user_id", "")
            if parent_state
            else ""
        )

        state: HarnessState = HarnessState(
            messages=messages,
            thread_id=thread_id,
            user_id=user_id,
        )

        try:
            result = await self._graph.ainvoke(state, RunnableConfig())
            msgs = result.get("messages", [])
            from langchain_core.messages import AIMessage

            # ── 修复 #13: 统计实际 AIMessage 数量作为迭代计数 ──
            iterations = sum(1 for m in msgs if isinstance(m, AIMessage))

            if msgs:
                last_msg = msgs[-1]
                if isinstance(last_msg, AIMessage) and not last_msg.tool_calls:
                    return SubAgentResult(
                        status="success",
                        output=str(last_msg.content),
                        iterations=iterations,
                    )
                # If last message is AIMessage with tool_calls, it means
                # create_agent stopped before completing (e.g. max turns)
                return SubAgentResult(
                    status="max_iterations_reached",
                    output=str(last_msg.content) if hasattr(last_msg, "content") else "",
                    iterations=self.config.max_turns,
                )

            return SubAgentResult(status="success", output="", iterations=0)
        except Exception as exc:
            logger.exception("SubAgent '%s' execution failed", self.config.name)
            return SubAgentResult(status="error", output=str(exc), iterations=0)
