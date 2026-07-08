"""Memory updater for reading, writing, and updating memory data — adapted from DeerFlow."""

import copy
import json
import logging
import os
import re
import uuid
from typing import Any

from langchain_openai import ChatOpenAI

from harness.config import load_config
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


def _create_memory_model(model_name: str | None = None) -> ChatOpenAI:
    """Create a lightweight model for memory updates (thinking disabled).

    Priority:
        1. HARNESS_OPENAI_API_KEY / HARNESS_OPENAI_BASE_URL env vars
        2. OPENAI_API_KEY / OPENAI_BASE_URL env vars
        3. HarnessConfig (loaded from config.yaml and harness/.env)
    """
    cfg = load_config()
    name = model_name or os.getenv("HARNESS_DEFAULT_MODEL", cfg.default_model)
    api_key = os.getenv(
        "HARNESS_OPENAI_API_KEY",
        os.getenv("OPENAI_API_KEY", cfg.openai_api_key),
    )
    base_url = os.getenv(
        "HARNESS_OPENAI_BASE_URL",
        os.getenv("OPENAI_BASE_URL", cfg.openai_base_url),
    )
    return ChatOpenAI(
        model=name,
        api_key=api_key,
        base_url=base_url,
        temperature=0.3,
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

    def _get_model(self) -> ChatOpenAI:
        return _create_memory_model(self._model_name)

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
                             user_id=None, metadata=None) -> bool:
        """Async entry point — may write to file, mem0, or both (dual-write).

        Routing logic:
        - backend=file + mem0_tool_enabled=false → only file (original behavior)
        - backend=file + mem0_tool_enabled=true  → BOTH file and mem0 (dual-write)
        - backend=mem0 + mem0_tool_enabled=false → only mem0 (original behavior)
        - backend=mem0 + mem0_tool_enabled=true  → only mem0 (no need for file)
        """
        from harness.config.memory_config import get_memory_config

        cfg = get_memory_config()
        mem0_tool_enabled = getattr(cfg, "mem0_tool_enabled", False)

        results: list[bool] = []

        # ── file 写入（当 backend=file 时）──
        if cfg.backend == "file":
            try:
                file_result = await self._do_update_memory(
                    messages=messages, thread_id=thread_id, agent_name=agent_name,
                    correction_detected=correction_detected,
                    reinforcement_detected=reinforcement_detected,
                    user_id=user_id,
                )
                results.append(file_result)
            except Exception as e:
                logger.error("file memory update failed: %s", e)
                results.append(False)

        # ── mem0 写入（当 backend=mem0 或 mem0_tool_enabled=true 时）──
        if cfg.backend == "mem0" or mem0_tool_enabled:
            try:
                mem0_result = await self._update_mem0(
                    messages, user_id, agent_name, thread_id,
                    correction_detected, reinforcement_detected, metadata,
                )
                results.append(mem0_result)
            except Exception as e:
                logger.error("mem0 update failed: %s", e)
                results.append(False)

        # 至少一个成功就算成功
        return any(results) if results else False

    async def _update_mem0(self, messages, user_id, agent_name, thread_id,
                           correction_detected, reinforcement_detected, metadata) -> bool:
        """mem0 backend：直接调 mem0.add()，内部含 LLM 提取+冲突检测。"""
        import asyncio

        from harness.memory.mem0_client import get_mem0

        mem0 = get_mem0()
        if mem0 is None:
            logger.error("mem0 backend enabled but client not initialized")
            return False

        # 转换消息格式为 mem0 期望的 [{"role":..., "content":...}]
        mem0_messages = []
        for m in messages:
            role = "user" if getattr(m, "type", None) == "human" else "assistant"
            content = m.content if isinstance(m.content, str) else str(m.content)
            if content.strip():
                mem0_messages.append({"role": role, "content": content})

        if not mem0_messages:
            return False

        # 构建 metadata
        mem_metadata: dict = {"thread_id": thread_id or ""}
        if metadata:
            mem_metadata.update(metadata)

        try:
            await asyncio.to_thread(
                mem0.add,
                mem0_messages,
                user_id=user_id or "default",
                agent_id=agent_name,
                metadata=mem_metadata,
            )
            logger.info(
                "mem0 add succeeded for user=%s agent=%s thread=%s",
                user_id, agent_name, thread_id,
            )
            return True
        except Exception as e:
            logger.error("mem0 add failed: %s", e)
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
        for section in ["workContext", "personalContext", "topOfMind"]:
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

        # Enforce max facts
        if len(current_memory["facts"]) > config.max_facts:
            current_memory["facts"] = sorted(
                current_memory["facts"],
                key=lambda f: f.get("confidence", 0), reverse=True,
            )[:config.max_facts]

        return current_memory
