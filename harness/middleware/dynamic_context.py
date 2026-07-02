"""DynamicContextMiddleware — inject memory and current date as a <system-reminder>.

使用 file backend（memory.json）：
- 首回合注入完整 reminder（memory + date）
- 跨午夜只更新日期
- 同日续聊不注入（frozen snapshot persists）

Reminder format:

    <system-reminder>
    <memory>...</memory>

    <current_date>2026-07-02, Thursday</current_date>
    </system-reminder>

Date-update format (midnight crossing):

    <system-reminder>
    <current_date>2026-07-03, Friday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import override

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.memory.prompt import format_memory_for_injection
from harness.memory.updater import get_memory_data
from harness.config.memory_config import get_memory_config
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"<current_date>([^<]+)</current_date>")
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"
_SUMMARY_MESSAGE_NAME = "summary"


def _extract_date(content: str) -> str | None:
    m = _DATE_RE.search(content)
    return m.group(1) if m else None


def is_dynamic_context_reminder(message: object) -> bool:
    return isinstance(message, HumanMessage) and bool(
        message.additional_kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY)
    )


def _last_injected_date(messages: list) -> str | None:
    for msg in reversed(messages):
        if is_dynamic_context_reminder(msg):
            content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
            return _extract_date(content_str)
    return None


def _is_user_injection_target(message: object) -> bool:
    return (
        isinstance(message, HumanMessage)
        and not is_dynamic_context_reminder(message)
        and message.name != _SUMMARY_MESSAGE_NAME
    )


class DynamicContextMiddleware(HarnessAgentMiddleware):
    """Inject memory (from memory.json) and current date into HumanMessages as a <system-reminder>.

    First turn: prepends full reminder (memory + date) to the first HumanMessage.
    Midnight crossing: injects lightweight date-update reminder.
    Same-day continuation: no injection needed (frozen snapshot persists).
    """

    name = "dynamic_context"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None):
        super().__init__(config)
        self._agent_name = agent_name

    # ── Reminder builders ────────────────────────────────────────────────

    def _build_full_reminder(self, *, user_id: str | None = None) -> tuple[str, str]:
        """Build the full reminder and return (reminder_text, memory_context_text)."""
        mem_cfg = get_memory_config()
        injection_enabled = mem_cfg.injection_enabled
        memory_context = ""
        memory_block = ""
        if injection_enabled:
            try:
                memory_data = get_memory_data(self._agent_name, user_id=user_id)
                memory_context = format_memory_for_injection(
                    memory_data,
                    max_tokens=mem_cfg.max_injection_tokens,
                )
                if memory_context:
                    memory_block = f"<memory>\n{memory_context}\n</memory>\n\n"
            except Exception as exc:
                logger.warning("Failed to load memory for injection: %s", exc)

        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        reminder = (
            f"<system-reminder>\n"
            f"{memory_block}"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )
        return reminder, memory_context

    def _build_date_update_reminder(self) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return (
            f"<system-reminder>\n"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    @staticmethod
    def _make_reminder_and_user_messages(
        original: HumanMessage, reminder_content: str,
    ) -> tuple[HumanMessage, HumanMessage]:
        """ID-swap: reminder takes original ID, user gets derived ID."""
        stable_id = original.id or str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=reminder_content,
            id=stable_id,
            additional_kwargs={
                "hide_from_ui": True,
                _DYNAMIC_CONTEXT_REMINDER_KEY: True,
            },
        )
        user_msg = HumanMessage(
            content=original.content,
            id=f"{stable_id}__user",
            name=original.name,
            additional_kwargs=original.additional_kwargs,
        )
        return reminder_msg, user_msg

    # ── Injection logic ──────────────────────────────────────────────────

    def _inject(self, state: HarnessState) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        user_id: str | None = state.get("user_id")
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        last_date = _last_injected_date(messages)

        if last_date is None:
            # First turn: inject full reminder
            first_idx = next(
                (i for i, m in enumerate(messages) if _is_user_injection_target(m)),
                None,
            )
            if first_idx is None:
                return None
            full_reminder, memory_context = self._build_full_reminder(user_id=user_id)
            logger.info(
                "DynamicContextMiddleware: injecting full reminder (len=%d, has_memory=%s, user_id=%s)",
                len(full_reminder),
                "<memory>" in full_reminder,
                user_id or "default",
            )
            reminder_msg, user_msg = self._make_reminder_and_user_messages(
                messages[first_idx], full_reminder,
            )
            return {
                "messages": [reminder_msg, user_msg],
                "memory_context": memory_context,
            }

        if last_date == current_date:
            # Same day: nothing to do
            return None

        # Midnight crossed: inject date-update reminder
        last_human_idx = next(
            (i for i in reversed(range(len(messages)))
             if _is_user_injection_target(messages[i])),
            None,
        )
        if last_human_idx is None:
            return None

        reminder_msg, user_msg = self._make_reminder_and_user_messages(
            messages[last_human_idx], self._build_date_update_reminder(),
        )
        logger.info(
            "DynamicContextMiddleware: midnight crossing — injected date update (user_id=%s)",
            user_id or "default",
        )
        return {"messages": [reminder_msg, user_msg]}

    @override
    async def abefore_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        return self._inject(state)
