"""Multi-Agent Workbench Harness — backend runtime for multi-agent collaboration.

Architecture:
- LangGraph-based state graph execution engine
- 14-layer onion middleware chain (DeerFlow 2.0 pattern)
- Lead Agent + SubAgent manager with concurrency control
- gbrain-style 4-layer memory system
- LLM-as-a-Judge quality evaluation
- Langfuse observability integration
- MCP tool adapter
- SSE streaming message bus
"""

from harness.config import HarnessConfig, load_config
from harness.models import (
    HarnessState,
    SubAgentConfig,
    SubAgentResult,
    ExecutionGraph,
    AgentNode,
    TokenUsage,
    ClarificationRequest,
    TodoItem,
    EvaluationResult,
    EvaluationCriteria,
    SubAgentEvaluation,
    MemorySignal,
    initial_state,
)

__version__ = "2.0.0"

# Lazy import to avoid circular dependency when running `python -m harness.main`
_HARNESS_SERVICE = None


def __getattr__(name: str):
    if name == "HarnessService":
        global _HARNESS_SERVICE
        if _HARNESS_SERVICE is None:
            from harness.main import HarnessService as _HS

            _HARNESS_SERVICE = _HS
        return _HARNESS_SERVICE
    raise AttributeError(f"module 'harness' has no attribute {name!r}")


__all__ = [
    "HarnessConfig",
    "load_config",
    "HarnessService",
    "HarnessState",
    "SubAgentConfig",
    "SubAgentResult",
    "ExecutionGraph",
    "AgentNode",
    "TokenUsage",
    "ClarificationRequest",
    "TodoItem",
    "EvaluationResult",
    "EvaluationCriteria",
    "SubAgentEvaluation",
    "MemorySignal",
    "initial_state",
]
