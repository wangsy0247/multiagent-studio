"""Harness middleware package — 17-layer middleware matching DeerFlow design.

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
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.middleware.summarization import SummarizationMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.title import TitleMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware
from harness.middleware.clarification import ClarificationMiddleware

# Strict registration order (matches DeerFlow spec)
#   [0-2]  Sandbox infrastructure
#   [3-5]  wrap_model_call onion: LLMError → LoopDetection → DanglingToolCall
#          (LLMError = outermost, DanglingToolCall = innermost closest to LLM)
#   [6]    Guardrail
#   [7]    ToolErrorHandling (always)
#   [8]    DynamicContext (date + memory injection)
#   [9]    Summarization
#   [10]   Todo (Plan Mode)
#   [11]   TokenUsage
#   [12]   Auto Title        ← after_model (反向)
#   [13]   Memory            ← after_agent (反向)
#   [14]   ViewImage
#   [15]   SubagentLimit
#   [16]   LoopDetection     ← (wrap_model_call 已在前, 这里是 before/after 钩子)
#   [17]   Clarification     ← after_model + after_agent (always last)
AGENT_MIDDLEWARE_ORDER: list[type[HarnessAgentMiddleware]] = [
    # [0-2] Sandbox infrastructure
    ThreadDataMiddleware,
    UploadsMiddleware,
    SandboxMiddleware,
    # [3-5] wrap_model_call onion (outermost → innermost)
    LLMErrorHandlingMiddleware,     # outermost wrapper
    LoopDetectionMiddleware,         # middle: cycle detection in wrap
    DanglingToolCallMiddleware,     # innermost: closest to LLM
    # [6] Guardrail
    GuardrailMiddleware,
    # [7] ToolErrorHandling
    ToolErrorHandlingMiddleware,
    # [8] DynamicContext
    DynamicContextMiddleware,
    # [9] Summarization
    SummarizationMiddleware,
    # [10] Todo
    TodoMiddleware,
    # [11] TokenUsage
    TokenUsageMiddleware,
    # [12] Title (aafter_model)
    TitleMiddleware,
    # [13] Memory (aafter_agent)
    MemoryMiddleware,
    # [14] ViewImage
    ViewImageMiddleware,
    # [15] SubagentLimit
    SubagentLimitMiddleware,
    # [16] Clarification (always last in after_model/after_agent reverse chain)
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
    "ToolErrorHandlingMiddleware",
    "DynamicContextMiddleware",
    "SummarizationMiddleware",
    "TodoMiddleware",
    "TokenUsageMiddleware",
    "TitleMiddleware",
    "MemoryMiddleware",
    "ViewImageMiddleware",
    "SubagentLimitMiddleware",
    "LoopDetectionMiddleware",
    "ClarificationMiddleware",
]
