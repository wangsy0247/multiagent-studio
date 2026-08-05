"""HarnessGraphFactory — build the execution graph using create_agent().

Architecture (aligned with the harness design):

    Inner graph (create_agent):
        Handles the core model ↔ tools ReAct loop with all middleware
        as AgentMiddleware instances.  Tool execution (including ``task``
        for SubAgent dispatch) happens inline within the agent's tool node.

        Memory updates happen via MemoryMiddleware.after_agent (queued, debounced).
        Memory injection happens via DynamicContextMiddleware.before_agent.

    Outer graph (StateGraph):
        START → agent (create_agent subgraph) → END

        No separate memory_update node — memory is entirely middleware-driven.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)


class HarnessGraphFactory:
    """Build the full execution graph from components.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM instance for the lead agent.
    tools : list[BaseTool]
        All tools available to the lead agent (including ``task``, ``ask_clarification``).
    middlewares : list[HarnessAgentMiddleware]
        The middleware instances in strict registration order.
    system_prompt : str
        Lead agent system prompt.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        tools: list[BaseTool],
        middlewares: list[HarnessAgentMiddleware],
        system_prompt: str,
        checkpointer: BaseCheckpointSaver | None = None,
    ):
        self.llm = llm
        self.tools = tools
        self.middlewares = middlewares
        self.system_prompt = system_prompt
        self.checkpointer = checkpointer

    def build(self) -> Any:
        """Compile and return the full execution graph.

        Returns a ``CompiledStateGraph`` that can be invoked with
        ``graph.ainvoke(state, config)``.
        """
        # ── Inner agent: create_agent handles the ReAct loop ──
        inner_agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=self.system_prompt,
            middleware=self.middlewares,
            state_schema=HarnessState,
        )

        # ── Outer graph: agent → END (memory is middleware-driven) ──
        graph = StateGraph(HarnessState)

        graph.add_node("agent", inner_agent)
        graph.set_entry_point("agent")
        graph.add_edge("agent", END)

        compiled = graph.compile(checkpointer=self.checkpointer)
        logger.info(
            "HarnessGraph compiled — model=%s tools=%d middlewares=%d checkpointer=%s",
            getattr(self.llm, "model_name", str(self.llm)),
            len(self.tools),
            len(self.middlewares),
            type(self.checkpointer).__name__ if self.checkpointer else "none",
        )
        return compiled


def build_harness_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    middlewares: list[HarnessAgentMiddleware],
    system_prompt: str,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Convenience function — build the full harness graph.

    Args:
        llm: Lead agent LLM.
        tools: All tools (including task, ask_clarification, core tools).
        middlewares: middleware instances in order.
        system_prompt: System prompt string.
        checkpointer: Optional LangGraph checkpointer for state persistence.

    Returns:
        Compiled LangGraph StateGraph ready for ``ainvoke``.
    """
    factory = HarnessGraphFactory(
        llm=llm,
        tools=tools,
        middlewares=middlewares,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
    )
    return factory.build()
