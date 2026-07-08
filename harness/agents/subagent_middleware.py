"""SubAgent middleware builder — stripped-down chain (7-9 middlewares).

Aligns with DeerFlow's ``build_subagent_runtime_middlewares()``.  SubAgents
do NOT need Uploads, Summarization, Todo, Title, Memory, DeferredToolFilter,
SubagentLimit, LoopDetection, Clarification, or DynamicContext middlewares.

See docs/subagent-refactor-plan.md §2.2 for the full rationale.
"""
from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware


def build_subagent_middlewares(
    *,
    vision_enabled: bool = False,
    guardrail_enabled: bool = False,
    tool_max_retries: int = 3,
) -> list[AgentMiddleware]:
    """Build a minimal middleware chain for SubAgent execution (7-9 items).

    Only includes middlewares that are **essential** for isolated task execution.
    Excludes anything related to: uploads, summarization, plan-mode, title
    generation, memory persistence, deferred-tool loading, subagent nesting,
    loop detection, and clarification interception.

    Parameters
    ----------
    vision_enabled : bool
        When True, appends ViewImageMiddleware for vision-capable models.
    guardrail_enabled : bool
        When True, inserts GuardrailMiddleware before SandboxAuditMiddleware.
    tool_max_retries : int
        Passed to ToolErrorHandlingMiddleware.
    """
    middlewares: list[AgentMiddleware] = [
        ThreadDataMiddleware(lazy_init=True),
        SandboxMiddleware(),
        DanglingToolCallMiddleware(),
        LLMErrorHandlingMiddleware(),
    ]

    # ---- optional: guardrail (inserted before sandbox audit) ----
    if guardrail_enabled:
        from harness.middleware.guardrail import GuardrailMiddleware
        middlewares.append(GuardrailMiddleware())

    middlewares.append(SandboxAuditMiddleware())
    middlewares.append(
        ToolErrorHandlingMiddleware({"max_retries": tool_max_retries})
    )

    # ---- optional: vision ----
    if vision_enabled:
        from harness.middleware.view_image import ViewImageMiddleware
        middlewares.append(ViewImageMiddleware())

    # ---- always last: safety termination guard ----
    middlewares.append(SafetyFinishReasonMiddleware())

    return middlewares


# Re-export SandboxMiddleware so the builder doesn't need a separate import
from harness.middleware.sandbox import SandboxMiddleware  # noqa: E402, F811
