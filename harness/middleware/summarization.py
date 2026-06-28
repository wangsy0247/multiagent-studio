"""SummarizationMiddleware — extends LangChain's SummarizationMiddleware with hook support.

Adapted from DeerFlow: adds ``before_summarization`` hooks so that
``memory_flush_hook`` can save conversation context to memory before
messages are compressed away.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol, override, runtime_checkable

from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware as LangChainSummarizationMiddleware
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

from harness.config.summarization_config import get_summarization_config
from harness.middleware.dynamic_context import is_dynamic_context_reminder

logger = logging.getLogger(__name__)


# ── Hook protocol ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SummarizationEvent:
    """Context emitted before conversation history is summarized away."""

    messages_to_summarize: tuple[Any, ...]
    preserved_messages: tuple[Any, ...]
    thread_id: str | None
    agent_name: str | None
    runtime: Runtime


@runtime_checkable
class BeforeSummarizationHook(Protocol):
    """Hook invoked before summarization removes messages from state."""

    def __call__(self, event: SummarizationEvent) -> None: ...


# ── Helpers ──────────────────────────────────────────────────────────────

def _resolve_thread_id(runtime: Runtime) -> str | None:
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        try:
            config_data = get_config()
        except RuntimeError:
            return None
        thread_id = config_data.get("configurable", {}).get("thread_id")
    return thread_id


def _resolve_agent_name(runtime: Runtime) -> str | None:
    agent_name = runtime.context.get("agent_name") if runtime.context else None
    if agent_name is None:
        try:
            config_data = get_config()
        except RuntimeError:
            return None
        agent_name = config_data.get("configurable", {}).get("agent_name")
    return agent_name


# ── DeerFlow-aligned SummarizationMiddleware ──────────────────────────────

class SummarizationMiddleware(LangChainSummarizationMiddleware):
    """LangChain SummarizationMiddleware extended with pre-summarization hooks.

    Hooks (e.g. ``memory_flush_hook``) are invoked just before messages
    are compressed, giving them a chance to persist important information.

    Accepts the same configuration format as DeerFlow via
    ``SummarizationConfig``.
    """

    name = "summarization"

    def __init__(
        self,
        *args,
        before_summarization: list[BeforeSummarizationHook] | None = None,
        preserve_dynamic_context_reminders: bool = True,
        **kwargs,
    ) -> None:
        """Initialize the summarization middleware.

        Args:
            *args: Passed through to LangChain's SummarizationMiddleware.
            before_summarization: Optional list of hook callables invoked
                before each summarization cycle.
            preserve_dynamic_context_reminders: If True, keep hidden
                dynamic-context reminders out of summary compression.
            **kwargs: Passed through to LangChain's SummarizationMiddleware.
        """
        super().__init__(*args, **kwargs)
        self._before_summarization_hooks = before_summarization or []
        self._preserve_dynamic_context_reminders_enabled = preserve_dynamic_context_reminders

    @override
    def _build_new_messages(self, summary: str) -> list[HumanMessage]:
        """Summary message hidden from UI but visible to the model."""
        return [
            HumanMessage(
                content=f"Here is a summary of the conversation to date:\n\n{summary}",
                name="summary",
            )
        ]

    def _preserve_dynamic_context_reminders(
        self,
        messages_to_summarize: list[AnyMessage],
        preserved_messages: list[AnyMessage],
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """Keep hidden dynamic-context reminders out of summary compression.

        These reminders carry the current date and optional memory. If
        summarization removes them, DynamicContextMiddleware can mistake the
        summary HumanMessage for the first user message and inject the reminder
        in the wrong place.
        """
        if not self._preserve_dynamic_context_reminders_enabled:
            return messages_to_summarize, preserved_messages

        reminders = [msg for msg in messages_to_summarize if is_dynamic_context_reminder(msg)]
        if not reminders:
            return messages_to_summarize, preserved_messages

        remaining = [msg for msg in messages_to_summarize if not is_dynamic_context_reminder(msg)]
        return remaining, reminders + preserved_messages

    def _fire_hooks(
        self,
        messages_to_summarize: list[Any],
        preserved_messages: list[Any],
        runtime: Runtime,
    ) -> None:
        """Invoke all registered before_summarization hooks."""
        if not self._before_summarization_hooks:
            return

        event = SummarizationEvent(
            messages_to_summarize=tuple(messages_to_summarize),
            preserved_messages=tuple(preserved_messages),
            thread_id=_resolve_thread_id(runtime),
            agent_name=_resolve_agent_name(runtime),
            runtime=runtime,
        )

        for hook in self._before_summarization_hooks:
            try:
                hook(event)
            except Exception:
                hook_name = getattr(hook, "__name__", None) or type(hook).__name__
                logger.exception("before_summarization hook %s failed", hook_name)

    @override
    def _maybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Override to fire hooks before summarization."""
        messages = list(state["messages"])
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(messages, cutoff_index)
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(
            messages_to_summarize, preserved_messages
        )
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)

        # Call parent implementation for actual summarization
        return super()._maybe_summarize(state, runtime)

    @override
    async def _amaybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Async override to fire hooks before summarization."""
        messages = list(state["messages"])
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(messages, cutoff_index)
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(
            messages_to_summarize, preserved_messages
        )
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)

        # Call parent implementation for actual summarization
        return await super()._amaybe_summarize(state, runtime)


# ── Factory ──────────────────────────────────────────────────────────────

def create_summarization_middleware(
    *,
    before_summarization: list[BeforeSummarizationHook] | None = None,
) -> SummarizationMiddleware:
    """Build a SummarizationMiddleware from SummarizationConfig."""
    cfg = get_summarization_config()
    if not cfg.enabled:
        return None

    # Build trigger and keep args in LangChain format
    trigger = None
    if cfg.trigger:
        if isinstance(cfg.trigger, list):
            trigger = [t.to_tuple() for t in cfg.trigger]
        else:
            trigger = cfg.trigger.to_tuple()

    keep = cfg.keep.to_tuple() if cfg.keep else ("messages", 20)

    return SummarizationMiddleware(
        trigger=trigger,
        keep=keep,
        trim_tokens_to_summarize=cfg.trim_tokens_to_summarize,
        summary_prompt=cfg.summary_prompt,
        model=cfg.model_name,
        before_summarization=before_summarization,
        preserve_dynamic_context_reminders=cfg.preserve_dynamic_context_reminders,
    )
