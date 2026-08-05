"""Harness middleware package — 20-layer middleware matching the harness design.

Execution order for each hook type:
  - before_agent / before_model: forward (index 0 → N)
  - wrap_model_call: nested (later registered = outer wrapper, inner runs first)
  - after_model / after_agent: reverse (index N → 0)
"""

from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.uploads import UploadsMiddleware
from harness.middleware.sandbox import SandboxMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.guardrail import GuardrailMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.middleware.summarization import SummarizationMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.title import TitleMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.clarification import ClarificationMiddleware

# Strict registration order (matches harness spec)
#   [0-2]  Sandbox infrastructure
#   [3-5]  wrap_model_call onion: LLMError → LoopDetection → DanglingToolCall
#   [6]    Guardrail (awrap_tool_call)
#   [7]    SandboxAudit (awrap_tool_call) ← NEW
#   [8]    ToolErrorHandling (awrap_tool_call)
#   [9]    DynamicContext (abefore_agent — date + memory injection)
#   [10]   Summarization (abefore_model)
#   [11]   Todo (Plan Mode)
#   [12]   TokenUsage (aafter_model)
#   [13]   Title (aafter_model)
#   [14]   Memory (aafter_agent)
#   [15]   ViewImage (abefore_model)
#   [16]   DeferredToolFilter (wrap_model_call + wrap_tool_call) ← NEW
#   [17]   SubagentLimit (aafter_model)
#   [18]   SafetyFinishReason (aafter_model) ← NEW
#   [19]   Clarification (always last, wrap_tool_call)
AGENT_MIDDLEWARE_ORDER: list[type[HarnessAgentMiddleware]] = [
    # [0-2] Sandbox infrastructure
    ThreadDataMiddleware,
    UploadsMiddleware,
    SandboxMiddleware,
    # [3-4] wrap_model_call innermost pair (matches the canonical design: Dangling innermost → LLMError wraps it)
    DanglingToolCallMiddleware,     # innermost: closest to LLM — patches missing ToolMessages
    LLMErrorHandlingMiddleware,     # wraps DanglingToolCall — catches LLM exceptions
    # [5] Guardrail (awrap_tool_call)
    GuardrailMiddleware,
    # [6] SandboxAudit (awrap_tool_call)
    SandboxAuditMiddleware,
    # [7] ToolErrorHandling (awrap_tool_call)
    ToolErrorHandlingMiddleware,
    # [8] DynamicContext (abefore_agent — date + memory)
    DynamicContextMiddleware,
    # [9] Summarization (abefore_model)
    SummarizationMiddleware,
    # [10] Todo (Plan Mode)
    TodoMiddleware,
    # [11] TokenUsage (aafter_model)
    TokenUsageMiddleware,
    # [12] Title (aafter_model)
    TitleMiddleware,
    # [13] Memory (aafter_agent)
    MemoryMiddleware,
    # [14] ViewImage (abefore_model)
    ViewImageMiddleware,
    # [15] DeferredToolFilter (wrap_model_call + wrap_tool_call)
    DeferredToolFilterMiddleware,
    # [16] SubagentLimit (aafter_model)
    SubagentLimitMiddleware,
    # [17] LoopDetection (wrap_model_call outermost + aafter_model)
    #      Registered right before SafetyFinishReason so that in the
    #      reverse-order aafter_model chain: Safety runs FIRST (sees raw
    #      response, strips safety tool_calls), then Loop accounts against
    #      the cleaned message.  For wrap_model_call, being registered
    #      last among model wrappers makes it outermost — it injects
    #      pending loop warnings right before the actual LLM call.
    LoopDetectionMiddleware,
    # [18] SafetyFinishReason (aafter_model — registered after LoopDetection
    #      so that LangChain's reverse-order after_model dispatch runs Safety first)
    SafetyFinishReasonMiddleware,
    # [19] Clarification (always last — intercepts ask_clarification via wrap_tool_call)
    ClarificationMiddleware,
]

__all__ = [
    "HarnessAgentMiddleware",
    "AGENT_MIDDLEWARE_ORDER",
    "ThreadDataMiddleware",
    "UploadsMiddleware",
    "SandboxMiddleware",
    "LLMErrorHandlingMiddleware",
    "DanglingToolCallMiddleware",
    "GuardrailMiddleware",
    "SandboxAuditMiddleware",
    "ToolErrorHandlingMiddleware",
    "DynamicContextMiddleware",
    "SummarizationMiddleware",
    "TodoMiddleware",
    "TokenUsageMiddleware",
    "TitleMiddleware",
    "MemoryMiddleware",
    "ViewImageMiddleware",
    "DeferredToolFilterMiddleware",
    "SubagentLimitMiddleware",
    "LoopDetectionMiddleware",
    "SafetyFinishReasonMiddleware",
    "ClarificationMiddleware",
]
