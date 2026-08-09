"""Memory updater for reading, writing, and updating memory data — adapted from harness."""

import copy
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_openai import ChatOpenAI

from harness.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
)
from harness.memory.storage import (
    create_empty_memory,
    get_memory_storage,
    utc_now_iso_z,
)
from harness.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


def _create_memory_model(
    model_name: str | None = None,
    *,
    api_key: str = "",
    base_url: str = "",
) -> ChatOpenAI | None:
    """创建 memory 更新用的轻量模型.

    无有效凭证时返回 None — 调用方应优雅跳过 memory 更新.

    凭证优先级 (高到低):
    1. 显式传入的 ``api_key`` / ``base_url`` (来自 per-user GraphContext)
    2. ``MemoryConfig.api_key`` / ``MemoryConfig.base_url`` (全局单例)
    3. ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` 环境变量

    模型名称优先级:
    1. 显式传入的 ``model_name``
    2. ``MemoryConfig.model_name``
    3. ``DEFAULT_MODEL`` env var
    4. ``gpt-4o-mini`` hardcoded fallback
    """
    from harness.config.memory_config import get_memory_config
    mem_cfg = get_memory_config()
    name = model_name or mem_cfg.model_name or os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
    effective_api_key = api_key or mem_cfg.api_key or os.getenv("OPENAI_API_KEY", "")
    effective_base_url = base_url or mem_cfg.base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    if not effective_api_key:
        logger.warning(
            "Memory LLM skipped: no API key configured "
            "(set OPENAI_API_KEY in harness/.env on the server)"
        )
        return None

    logger.info(
        "Memory LLM: model=%s",
        name,
    )
    from harness.observability.usage_ledger import get_usage_ledger_callback

    return ChatOpenAI(
        model=name,
        api_key=effective_api_key,
        base_url=effective_base_url,
        temperature=0.3,
        request_timeout=60,
        max_retries=1,
        callbacks=[get_usage_ledger_callback()],
    )


def get_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    return get_memory_storage().load(agent_name, user_id=user_id)


def reload_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    return get_memory_storage().reload(agent_name, user_id=user_id)


def clear_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    cleared = create_empty_memory()
    if not get_memory_storage().save(cleared, agent_name, user_id=user_id):
        raise OSError("Failed to save cleared memory data")
    return cleared


# ── Upload mention stripping ──────────────────────────────────────────────
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _strip_upload_mentions_from_memory(memory_data: dict[str, Any]) -> dict[str, Any]:
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]
    return memory_data


def _fact_content_key(content: Any) -> str | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return stripped.casefold()


def _extract_text(content: Any) -> str:
    """Extract plain text from LLM response content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict):
                text_val = block.get("text")
                if isinstance(text_val, str):
                    pieces.append(text_val)
        return "\n".join(pieces)
    return str(content)


# ── MemoryUpdater ─────────────────────────────────────────────────────────
class MemoryUpdater:
    """Updates memory using LLM based on conversation context."""

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name
        # Per-request overrides (set by aupdate_memory)
        self._api_key: str = ""
        self._base_url: str = ""

    def _get_model(self) -> ChatOpenAI | None:
        return _create_memory_model(
            self._model_name, api_key=self._api_key, base_url=self._base_url,
        )

    def _build_correction_hint(self, correction_detected: bool, reinforcement_detected: bool) -> str:
        hint = ""
        if correction_detected:
            hint = (
                "IMPORTANT: Explicit correction signals were detected in this conversation. "
                "Pay special attention to what the agent got wrong, what the user corrected, "
                "and record the correct approach as a fact with category "
                '"correction" and confidence >= 0.95 when appropriate.'
            )
        if reinforcement_detected:
            rhint = (
                "IMPORTANT: Positive reinforcement signals were detected in this conversation. "
                "Record the confirmed approach, style, or preference as a fact with category "
                '"preference" or "behavior" and confidence >= 0.9 when appropriate.'
            )
            hint = (hint + "\n" + rhint).strip() if hint else rhint
        return hint

    def _prepare_update_prompt(self, messages, agent_name, correction_detected,
                               reinforcement_detected, user_id=None):
        config = get_memory_config()
        if not config.enabled or not messages:
            return None
        current_memory = get_memory_data(agent_name, user_id=user_id)
        conversation_text = format_conversation_for_update(messages)
        if not conversation_text.strip():
            return None
        correction_hint = self._build_correction_hint(correction_detected, reinforcement_detected)

        # ── Delta optimisation: send compact memory view ──
        # Instead of the full JSON (which grows linearly with facts),
        # send summaries + a trimmed fact list (most recent + highest confidence).
        compact_memory = self._compact_memory_for_prompt(current_memory)

        prompt = MEMORY_UPDATE_PROMPT.format(
            current_memory=json.dumps(compact_memory, indent=2, ensure_ascii=False),
            conversation=conversation_text,
            correction_hint=correction_hint,
        )
        return current_memory, prompt

    @staticmethod
    def _compact_memory_for_prompt(memory_data: dict[str, Any]) -> dict[str, Any]:
        """Return a compact view of memory for the LLM prompt.

        Summaries are always included (they're small). Facts are limited to
        the most recent + highest-confidence entries to keep token costs
        bounded regardless of total fact count.
        """
        facts: list[dict[str, Any]] = memory_data.get("facts", [])

        if len(facts) <= 25:
            return memory_data  # small enough; send as-is

        # Sort facts by confidence (desc), then by createdAt (desc) for recency bias
        scored = sorted(
            facts,
            key=lambda f: (
                f.get("confidence", 0.0),
                f.get("createdAt", ""),
            ),
            reverse=True,
        )

        # Take top 25 facts + add count hint
        compact_facts = scored[:25]
        omitted = len(facts) - len(compact_facts)

        compact = {**memory_data, "facts": compact_facts}
        if omitted > 0:
            compact["_facts_omitted"] = (
                f"{omitted} additional facts omitted from prompt "
                f"(total {len(facts)} stored). The full fact list is available "
                f"for deduplication — only add newFacts that are genuinely new."
            )
        return compact

    def _finalize_update(self, current_memory, response_content, thread_id, agent_name, user_id=None):
        response_text = _extract_text(response_content).strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        update_data = json.loads(response_text)
        updated_memory = self._apply_updates(copy.deepcopy(current_memory), update_data, thread_id)
        updated_memory = _strip_upload_mentions_from_memory(updated_memory)
        return get_memory_storage().save(updated_memory, agent_name, user_id=user_id)

    async def aupdate_memory(self, messages, thread_id=None, agent_name=None,
                             correction_detected=False, reinforcement_detected=False,
                             user_id=None, metadata=None, *,
                             api_key: str = "", base_url: str = "",
                             model_name: str = "") -> bool:
        """Async entry point — writes memory to file storage."""

        # Store per-user credentials for _get_model()
        self._api_key = api_key
        self._base_url = base_url
        if model_name:
            self._model_name = model_name

        try:
            return await self._do_update_memory(
                messages=messages, thread_id=thread_id, agent_name=agent_name,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
                user_id=user_id,
            )
        except Exception as e:
            logger.error("file memory update failed: %s", e)
            return False

    async def _do_update_memory(self, messages, thread_id=None, agent_name=None,
                                correction_detected=False, reinforcement_detected=False,
                                user_id=None) -> bool:
        """Async memory update core — ainvoke instead of invoke."""
        try:
            prepared = self._prepare_update_prompt(
                messages=messages, agent_name=agent_name,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
                user_id=user_id,
            )
            if prepared is None:
                return False
            current_memory, prompt = prepared
            model = self._get_model()
            if model is None:
                return False  # 无有效凭证, 跳过本次更新
            response = await model.ainvoke(prompt, config={"run_name": "memory_agent"})
            return self._finalize_update(
                current_memory=current_memory,
                response_content=response.content,
                thread_id=thread_id, agent_name=agent_name, user_id=user_id,
            )
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response for memory update: %s", e)
            return False
        except Exception as e:
            # ── 静默处理模型平台内容安全审查拒绝 ──
            error_code = getattr(e, "code", None)
            if error_code == "data_inspection_failed":
                logger.warning(
                    "Memory update skipped: content safety check failed (%s)",
                    getattr(e, "message", str(e)),
                )
                return False
            logger.exception("Memory update failed: %s", e)
            return False


    def _apply_updates(self, current_memory, update_data, thread_id=None):
        config = get_memory_config()
        now = utc_now_iso_z()

        # Update user sections
        user_updates = update_data.get("user", {})
        for section in ["workContext", "personalContext", "topOfMind", "avoidances"]:
            section_data = user_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["user"][section] = {
                    "summary": section_data["summary"], "updatedAt": now,
                }

        # Update history sections
        history_updates = update_data.get("history", {})
        for section in ["recentWeeks", "earlierContext", "longTermBackground"]:
            section_data = history_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["history"][section] = {
                    "summary": section_data["summary"], "updatedAt": now,
                }

        # Remove facts
        facts_to_remove = set(update_data.get("factsToRemove", []))
        if facts_to_remove:
            current_memory["facts"] = [
                f for f in current_memory.get("facts", [])
                if f.get("id") not in facts_to_remove
            ]

        # Add new facts
        existing_keys = {
            fk for fk in (
                _fact_content_key(f.get("content"))
                for f in current_memory.get("facts", [])
            ) if fk is not None
        }
        for fact in update_data.get("newFacts", []):
            confidence = fact.get("confidence", 0.5)
            if confidence >= config.fact_confidence_threshold:
                raw_content = fact.get("content", "")
                if not isinstance(raw_content, str):
                    continue
                normalized = raw_content.strip()
                fk = _fact_content_key(normalized)
                if fk is not None and fk in existing_keys:
                    continue
                entry = {
                    "id": f"fact_{uuid.uuid4().hex[:8]}",
                    "content": normalized,
                    "category": fact.get("category", "context"),
                    "confidence": confidence,
                    "createdAt": now,
                    "source": thread_id or "unknown",
                }
                source_error = fact.get("sourceError")
                if isinstance(source_error, str) and source_error.strip():
                    entry["sourceError"] = source_error.strip()
                current_memory["facts"].append(entry)
                if fk is not None:
                    existing_keys.add(fk)

        # ── TTL 过期清理 ──
        if config.memory_ttl_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=config.memory_ttl_days)
            cutoff_iso = cutoff.isoformat()
            before_count = len(current_memory["facts"])
            current_memory["facts"] = [
                f for f in current_memory["facts"]
                if f.get("createdAt", "") >= cutoff_iso
            ]
            removed = before_count - len(current_memory["facts"])
            if removed > 0:
                logger.info(
                    "Memory TTL cleanup: removed %d expired facts (ttl=%d days)",
                    removed, config.memory_ttl_days,
                )

        # ── 强制 max_facts: 保留最新的 N 条 (按 createdAt 降序) ──
        if len(current_memory["facts"]) > config.max_facts:
            current_memory["facts"] = sorted(
                current_memory["facts"],
                key=lambda f: f.get("createdAt", ""), reverse=True,
            )[:config.max_facts]

        return current_memory
