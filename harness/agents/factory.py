"""SDK-level factory — create_agent from plain Python arguments.

create_agent accepts plain Python arguments — no YAML files, no global
singletons. It sits between the raw langchain.agents.create_agent primitive
and the config-driven make_lead_agent application factory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents import create_agent as langchain_create_agent
from langchain.agents.middleware import AgentMiddleware

from harness.agents.features import RuntimeFeatures
from harness.agents.lead_agent import _build_middlewares as _app_build_middlewares
from harness.middleware import AGENT_MIDDLEWARE_ORDER as MW
from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware.clarification import ClarificationMiddleware
from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.middleware.guardrail import GuardrailMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.summarization import create_summarization_middleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.title import TitleMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.uploads import UploadsMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.sandbox import SandboxMiddleware
from harness.models import HarnessState

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def create_agent(
    model: BaseChatModel,
    tools: list[BaseTool] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    features: RuntimeFeatures | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
    plan_mode: bool = False,
    state_schema: type | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    name: str = "default",
) -> CompiledStateGraph:
    """Create a multiagent-studio agent from plain Python arguments.

    The factory assembly itself reads no config files.

    Parameters
    ----------
    model:
        Chat model instance.
    tools:
        User-provided tools. Feature-injected tools are appended automatically.
    system_prompt:
        System message. In SDK mode, this becomes the SOUL injected by
        DynamicContextMiddleware into the <system-reminder>.
    middleware:
        **Full takeover** — if provided, this exact list is used.
    features:
        Declarative feature flags. Cannot be combined with *middleware*.
    extra_middleware:
        Additional middlewares inserted via @Next/@Prev positioning.
    plan_mode:
        Enable TodoMiddleware for task tracking.
    state_schema:
        LangGraph state type. Defaults to HarnessState.
    checkpointer:
        Optional persistence backend.
    name:
        Agent name (passed to MemoryMiddleware and DynamicContextMiddleware).
    """
    if middleware is not None and features is not None:
        raise ValueError("Cannot specify both 'middleware' and 'features'. Use one or the other.")
    if middleware is not None and extra_middleware:
        raise ValueError("Cannot use 'extra_middleware' with 'middleware' (full takeover).")
    if extra_middleware:
        for mw in extra_middleware:
            if not isinstance(mw, AgentMiddleware):
                raise TypeError(f"extra_middleware items must be AgentMiddleware instances, got {type(mw).__name__}")

    effective_tools: list[BaseTool] = list(tools or [])
    effective_state = state_schema or HarnessState

    if middleware is not None:
        effective_middleware = list(middleware)
    else:
        feat = features or RuntimeFeatures()
        extra_mds = list(extra_middleware) if extra_middleware else []
        effective_middleware, extra_tools = _assemble_from_features(
            feat,
            name=name,
            system_prompt=system_prompt,
            plan_mode=plan_mode,
            extra_middleware=extra_mds,
        )
        existing_names = {t.name for t in effective_tools}
        for t in extra_tools:
            if t.name not in existing_names:
                effective_tools.append(t)
                existing_names.add(t.name)

    return langchain_create_agent(
        model=model,
        tools=effective_tools or None,
        middleware=effective_middleware,
        system_prompt=system_prompt,
        state_schema=effective_state,
        checkpointer=checkpointer,
        name=name,
    )


# ---------------------------------------------------------------------------
# Internal: feature-driven middleware assembly
# ---------------------------------------------------------------------------


def _assemble_from_features(
    feat: RuntimeFeatures,
    *,
    name: str = "default",
    system_prompt: str | None = None,
    plan_mode: bool = False,
    extra_middleware: list[AgentMiddleware] | None = None,
) -> tuple[list[AgentMiddleware], list[BaseTool]]:
    """Build an ordered middleware chain + extra tools from *feat*.

    Middleware order matches DeerFlow's make_lead_agent (20 middlewares):

      0-2. Sandbox infrastructure (ThreadData → Uploads → Sandbox)
      3-4. wrap_model_call innermost pair (DanglingToolCall → LLMError)
      5.   GuardrailMiddleware (guardrail feature)
      6.   SandboxAuditMiddleware (always)
      7.   ToolErrorHandlingMiddleware (tool_error_handling feature)
      8.   DynamicContextMiddleware (dynamic_context feature)
      9.   SummarizationMiddleware (summarization feature)
      10.  TodoMiddleware (plan_mode or todo feature)
      11.  TokenUsageMiddleware (token_usage feature)
      12.  TitleMiddleware (auto_title feature)
      13.  MemoryMiddleware (memory feature)
      14.  ViewImageMiddleware (vision feature)
      15.  DeferredToolFilterMiddleware (tool_search feature)
      16.  SubagentLimitMiddleware (subagent feature)
      17.  LoopDetectionMiddleware (loop_detection feature — outermost model wrapper)
      18.  SafetyFinishReasonMiddleware (always)
      19.  ClarificationMiddleware (always last)
    """
    chain: list[AgentMiddleware] = []
    extra_tools: list[BaseTool] = []
    from langchain_core.tools import BaseTool as BT

    # --- [0-2] Sandbox infrastructure ---
    if feat.sandbox is not False:
        if isinstance(feat.sandbox, AgentMiddleware):
            chain.append(feat.sandbox)
        else:
            chain.append(ThreadDataMiddleware(lazy_init=True))
            chain.append(UploadsMiddleware())
            chain.append(SandboxMiddleware())

    # --- [3-4] wrap_model_call innermost pair: Dangling closest to LLM → LLMError wraps it ---
    chain.append(DanglingToolCallMiddleware())
    chain.append(LLMErrorHandlingMiddleware())

    # --- [5] Guardrail ---
    if feat.guardrail is not False:
        if isinstance(feat.guardrail, AgentMiddleware):
            chain.append(feat.guardrail)
        else:
            raise ValueError("guardrail=True requires a custom AgentMiddleware instance")

    # --- [6] SandboxAudit (always) ---
    chain.append(SandboxAuditMiddleware())

    # --- [7] ToolErrorHandling ---
    if feat.tool_error_handling is not False:
        if isinstance(feat.tool_error_handling, AgentMiddleware):
            chain.append(feat.tool_error_handling)
        else:
            chain.append(ToolErrorHandlingMiddleware())

    # --- [8] DynamicContext (SOUL + memory + date) ---
    if feat.dynamic_context is not False:
        if isinstance(feat.dynamic_context, AgentMiddleware):
            chain.append(feat.dynamic_context)
        else:
            chain.append(DynamicContextMiddleware(agent_name=name, soul=system_prompt))

    # --- [9] Summarization ---
    if feat.summarization is not False:
        if isinstance(feat.summarization, AgentMiddleware):
            chain.append(feat.summarization)
        else:
            from harness.memory.summarization_hook import memory_flush_hook

            hooks = []
            if feat.memory is not False:
                hooks.append(memory_flush_hook)
            summ_mw = create_summarization_middleware(before_summarization=hooks)
            if summ_mw is not None:
                chain.append(summ_mw)

    # --- [10] Todo ---
    if plan_mode or (feat.todo is not False and feat.todo is not True):
        if isinstance(feat.todo, AgentMiddleware):
            chain.append(feat.todo)
        else:
            chain.append(TodoMiddleware())

    # --- [11] TokenUsage ---
    if feat.token_usage is not False:
        if isinstance(feat.token_usage, AgentMiddleware):
            chain.append(feat.token_usage)
        else:
            chain.append(TokenUsageMiddleware())

    # --- [12] Title ---
    if feat.auto_title is not False:
        if isinstance(feat.auto_title, AgentMiddleware):
            chain.append(feat.auto_title)
        else:
            chain.append(TitleMiddleware())

    # --- [13] Memory ---
    if feat.memory is not False:
        if isinstance(feat.memory, AgentMiddleware):
            chain.append(feat.memory)
        else:
            chain.append(MemoryMiddleware(agent_name=name))

    # --- [14] ViewImage ---
    if feat.vision is not False:
        if isinstance(feat.vision, AgentMiddleware):
            chain.append(feat.vision)
        else:
            chain.append(ViewImageMiddleware())

    # --- [15] DeferredToolFilter ---
    if feat.tool_search is not False:
        if isinstance(feat.tool_search, AgentMiddleware):
            chain.append(feat.tool_search)
        else:
            chain.append(DeferredToolFilterMiddleware())

    # --- [16] SubagentLimit ---
    if feat.subagent is not False:
        if isinstance(feat.subagent, AgentMiddleware):
            chain.append(feat.subagent)
        else:
            chain.append(SubagentLimitMiddleware())

    # --- [17] LoopDetection — outermost model wrapper + aafter_model ---
    if feat.loop_detection is not False:
        if isinstance(feat.loop_detection, AgentMiddleware):
            chain.append(feat.loop_detection)
        else:
            chain.append(LoopDetectionMiddleware())

    # --- [18] SafetyFinishReason (always) ---
    chain.append(SafetyFinishReasonMiddleware())

    # --- [19] Clarification (always last) ---
    if feat.clarification is not False:
        if isinstance(feat.clarification, AgentMiddleware):
            chain.append(feat.clarification)
        else:
            chain.append(ClarificationMiddleware())

    # --- Insert extra_middleware via @Next/@Prev ---
    if extra_middleware:
        _insert_extra(chain, extra_middleware)
        clar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
        if clar_idx != len(chain) - 1:
            chain.append(chain.pop(clar_idx))

    return chain, extra_tools


# ---------------------------------------------------------------------------
# Internal: extra middleware insertion with @Next/@Prev
# ---------------------------------------------------------------------------


def _insert_extra(chain: list[AgentMiddleware], extras: list[AgentMiddleware]) -> None:
    """Insert extra middlewares into *chain* using @Next/@Prev anchors.

    Algorithm:
      1. Validate: no middleware has both @Next and @Prev.
      2. Conflict detection: two extras targeting same anchor → error.
      3. Insert unanchored extras before ClarificationMiddleware.
      4. Insert anchored extras iteratively (supports cross-anchoring).
      5. ClarificationMiddleware invariant: always last.
    """
    next_targets: dict[type, type] = {}
    prev_targets: dict[type, type] = {}
    anchored: list[tuple[AgentMiddleware, str, type]] = []
    unanchored: list[AgentMiddleware] = []

    for mw in extras:
        next_anchor = getattr(type(mw), "_next_anchor", None)
        prev_anchor = getattr(type(mw), "_prev_anchor", None)

        if next_anchor and prev_anchor:
            raise ValueError(f"{type(mw).__name__} cannot have both @Next and @Prev")

        if next_anchor:
            if next_anchor in next_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} and {next_targets[next_anchor].__name__} both @Next({next_anchor.__name__})")
            if next_anchor in prev_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} @Next({next_anchor.__name__}) and {prev_targets[next_anchor].__name__} @Prev({next_anchor.__name__}) — use cross-anchoring between extras instead")
            next_targets[next_anchor] = type(mw)
            anchored.append((mw, "next", next_anchor))
        elif prev_anchor:
            if prev_anchor in prev_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} and {prev_targets[prev_anchor].__name__} both @Prev({prev_anchor.__name__})")
            if prev_anchor in next_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} @Prev({prev_anchor.__name__}) and {next_targets[prev_anchor].__name__} @Next({prev_anchor.__name__}) — use cross-anchoring between extras instead")
            prev_targets[prev_anchor] = type(mw)
            anchored.append((mw, "prev", prev_anchor))
        else:
            unanchored.append(mw)

    # Unanchored → before ClarificationMiddleware
    clarification_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
    for mw in unanchored:
        chain.insert(clarification_idx, mw)
        clarification_idx += 1

    # Anchored → iterative insertion
    pending = list(anchored)
    max_rounds = len(pending) + 1
    for _ in range(max_rounds):
        if not pending:
            break
        remaining = []
        for mw, direction, anchor in pending:
            idx = next((i for i, m in enumerate(chain) if isinstance(m, anchor)), None)
            if idx is None:
                remaining.append((mw, direction, anchor))
                continue
            if direction == "next":
                chain.insert(idx + 1, mw)
            else:
                chain.insert(idx, mw)
        if len(remaining) == len(pending):
            names = [type(m).__name__ for m, _, _ in remaining]
            raise ValueError(f"Cannot resolve positions for {', '.join(names)} — anchors not found in chain")
        pending = remaining
