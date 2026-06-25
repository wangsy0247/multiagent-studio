"""Declarative feature flags for agent middleware activation.

Matches DeerFlow ``RuntimeFeatures`` — each feature controls whether a
corresponding middleware is assembled into the chain.

Feature values:
  - ``True``: use the built-in default middleware
  - ``False``: disable the middleware entirely
  - ``HarnessAgentMiddleware`` instance: use this custom implementation

``summarization`` and ``guardrail`` have no built-in default — they only
accept ``False`` (disable) or a ``HarnessAgentMiddleware`` instance (custom).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from harness.middleware.base import HarnessAgentMiddleware


@dataclass
class RuntimeFeatures:
    """Declarative feature flags for middleware assembly.

    Default values match DeerFlow defaults:
    """

    # Sandbox infrastructure (ThreadData + Uploads + Sandbox bundled)
    sandbox: bool | HarnessAgentMiddleware = True
    # Always-on repair middlewares
    dangling_tool_call: bool = True
    tool_error_handling: bool = True
    # Guardrail (no built-in default — requires custom instance)
    guardrail: Literal[False] | HarnessAgentMiddleware = False
    # Dynamic context (date + memory injection before each model call)
    dynamic_context: bool = True
    # Summarization (no built-in default — requires custom instance with model)
    summarization: Literal[False] | HarnessAgentMiddleware = False
    # Plan Mode TODO
    todo: bool | HarnessAgentMiddleware = False
    # Token usage tracking
    token_usage: bool = True
    # Auto title generation
    auto_title: bool | HarnessAgentMiddleware = False
    # Memory injection + extraction
    memory: bool | HarnessAgentMiddleware = True
    # Image viewing (vision)
    vision: bool | HarnessAgentMiddleware = False
    # SubAgent concurrency control
    subagent: bool | HarnessAgentMiddleware = False
    # Loop detection
    loop_detection: bool | HarnessAgentMiddleware = True
    # Clarification (always last)
    clarification: bool | HarnessAgentMiddleware = True
    # Uploads (file upload context injection)
    uploads: bool | HarnessAgentMiddleware = True
