"""DynamicContextMiddleware — inject memory and current date as a <system-reminder>.

每轮都查都注入方案：
- 每轮 abefore_agent 用最新用户消息调 mem0.search()
- 首回合用组合查询（固定查询 + 首条消息）
- 后续回合用 RemoveMessage 删除上一轮的 reminder，追加新 reminder（无累积）
- 保留日期注入逻辑（跨午夜更新日期）
- file backend 保留原有首回合注入+冻结逻辑（向后兼容）

Reminder format:

    <system-reminder>
    <memory>...</memory>
    <current_date>2026-07-01, Tuesday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import override

from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
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
    """每轮都查都注入：每轮用最新消息 search mem0，RemoveMessage 替换旧 reminder。

    - mem0 backend: 每轮 search + RemoveMessage 替换，无累积
    - file backend: 首回合注入后冻结（向后兼容）
    """

    name = "dynamic_context"

    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None):
        super().__init__(config)
        self._agent_name = agent_name

    # ── mem0 检索 ────────────────────────────────────────────────────────

    def _search_mem0(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """同步调用 mem0.search()，返回记忆列表。"""
        from harness.memory.mem0_client import get_mem0, is_mem0_enabled

        if not is_mem0_enabled():
            return []

        mem0 = get_mem0()
        if mem0 is None:
            return []

        filters: dict = {"user_id": user_id}
        if self._agent_name:
            filters["agent_id"] = self._agent_name

        # 时间过滤暂不支持 Chroma（Chroma 的 $gte 需要 Unix timestamp，不是 ISO 字符串）
        # 若切换到 Qdrant/pgvector 等向量存储时可启用此功能
        # mem_cfg = get_memory_config()
        # if mem_cfg.mem0_enable_time_filter:
        #     cutoff = (
        #         datetime.now(UTC) - timedelta(days=mem_cfg.mem0_recent_days)
        #     ).isoformat().replace("+00:00", "Z")
        #     filters["created_at"] = {"gte": cutoff}

        try:
            results = mem0.search(
                query=query,
                filters=filters,
                top_k=top_k,
            )
            # mem0 返回格式：{"results": [{"id":..., "memory":..., "score":...}]}
            if isinstance(results, dict):
                return results.get("results", [])
            elif isinstance(results, list):
                return results
            return []
        except Exception as e:
            logger.warning("mem0 search failed: %s", e)
            return []

    async def _search_mem0_async(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """异步包装 mem0.search()，用 asyncio.to_thread 避免阻塞事件循环。"""
        return await asyncio.to_thread(self._search_mem0, query, user_id, top_k)

    async def _combined_search(self, first_message: str, user_id: str, top_k: int) -> list[dict]:
        """首回合组合查询：固定查询 + 首条消息，合并去重。"""
        mem_cfg = get_memory_config()

        # 并发两次 search
        general_task = self._search_mem0_async(mem_cfg.mem0_general_query, user_id, top_k)
        specific_task = self._search_mem0_async(first_message, user_id, top_k)
        general, specific = await asyncio.gather(general_task, specific_task)

        # 合并去重（按 memory id 或 content 前 50 字符）
        seen_ids: set[str] = set()
        merged: list[dict] = []
        for r in general + specific:
            mid = r.get("id") or r.get("memory", "")[:50]
            if mid not in seen_ids:
                seen_ids.add(mid)
                merged.append(r)

        # 按 score 排序，取 top_k
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:top_k]

    # ── 格式化记忆为注入文本 ──────────────────────────────────────────────

    def _format_memories(self, memories: list[dict]) -> str:
        """把 mem0 检索结果格式化为注入文本。"""
        if not memories:
            return ""

        lines: list[str] = []
        for m in memories:
            content = m.get("memory", "")
            if content:
                lines.append(f"- {content}")
        return "\n".join(lines) if lines else ""

    # ── 构建 reminder ────────────────────────────────────────────────────

    def _build_reminder(self, memories_text: str, *, is_update: bool = False) -> str:
        """构建 system-reminder 文本。"""
        current_date = datetime.now().strftime("%Y-%m-%d, %A")

        memory_block = ""
        if memories_text:
            tag = "memory_update" if is_update else "memory"
            memory_block = f"<{tag}>\n{memories_text}\n</{tag}>\n\n"

        return (
            f"<system-reminder>\n"
            f"{memory_block}"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    def _build_date_only_reminder(self) -> str:
        """仅更新日期的轻量 reminder。"""
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return (
            f"<system-reminder>\n"
            f"<current_date>{current_date}</current_date>\n"
            f"</system-reminder>"
        )

    # ── 消息操作工具 ──────────────────────────────────────────────────────

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

    def _get_latest_user_message(self, messages: list) -> HumanMessage | None:
        """获取最新的非 reminder、非 summary 用户消息。"""
        for msg in reversed(messages):
            if _is_user_injection_target(msg):
                return msg
        return None

    def _get_old_reminder_ids(self, messages: list) -> list[str]:
        """获取所有旧 reminder 的 message id（用于 RemoveMessage）。"""
        return [m.id for m in messages if is_dynamic_context_reminder(m)]

    # ── 主注入逻辑 ────────────────────────────────────────────────────────

    async def _inject(self, state: HarnessState) -> dict | None:
        """每轮都查都注入的核心逻辑。"""
        from harness.memory.mem0_client import is_mem0_enabled

        messages = list(state.get("messages", []))
        if not messages:
            return None

        user_id: str = state.get("user_id", "default")
        current_date = datetime.now().strftime("%Y-%m-%d, %A")

        mem_cfg = get_memory_config()

        # ── 分支 1：mem0 backend — 每轮都查都注入 ──
        if is_mem0_enabled() and mem_cfg.injection_enabled:
            return await self._inject_mem0(
                state, messages, user_id, current_date, mem_cfg,
            )

        # ── 分支 2：file backend（向后兼容）── 保留原有逻辑 ──
        return self._inject_file_legacy(state, messages, user_id, current_date, mem_cfg)

    async def _inject_mem0(
        self, state: HarnessState, messages: list,
        user_id: str, current_date: str, mem_cfg,
    ) -> dict | None:
        """mem0 backend：每轮 search + RemoveMessage 替换。"""

        # 找到最新的用户消息
        latest_user_msg = self._get_latest_user_message(messages)
        if latest_user_msg is None:
            return None

        # 获取旧 reminder ID（用于 RemoveMessage）
        old_reminder_ids = self._get_old_reminder_ids(messages)
        is_first_turn = len(old_reminder_ids) == 0

        # 获取最新用户消息内容
        user_content = latest_user_msg.content
        if not isinstance(user_content, str):
            user_content = str(user_content)

        # 检索记忆
        if is_first_turn:
            # 首回合：组合查询（固定查询 + 首条消息）
            memories = await self._combined_search(
                user_content, user_id, mem_cfg.mem0_search_top_k,
            )
            is_update = False
        else:
            # 后续回合：只用最新消息 search
            memories = await self._search_mem0_async(
                user_content, user_id, mem_cfg.mem0_search_top_k,
            )
            is_update = True

        # 格式化记忆
        memories_text = self._format_memories(memories)

        # 构建 reminder
        reminder_content = self._build_reminder(memories_text, is_update=is_update)

        # 构建新消息
        new_id = str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=reminder_content,
            id=new_id,
            additional_kwargs={
                "hide_from_ui": True,
                _DYNAMIC_CONTEXT_REMINDER_KEY: True,
            },
        )
        user_msg = HumanMessage(
            content=latest_user_msg.content,
            id=f"{new_id}__user",
            name=latest_user_msg.name,
            additional_kwargs=latest_user_msg.additional_kwargs,
        )

        # ── 构建消息操作列表 ──
        # 1) 删除旧 reminder + 旧 user 派生消息
        new_messages: list = []
        for rid in old_reminder_ids:
            new_messages.append(RemoveMessage(id=rid))
            new_messages.append(RemoveMessage(id=f"{rid}__user"))

        # 2) 删除被替换的原始用户消息（防止 "用户: xxx" 重复）
        orig_id = latest_user_msg.id
        if orig_id:
            new_messages.append(RemoveMessage(id=orig_id))

        # 3) 追加新 reminder + 新 user
        new_messages.append(reminder_msg)
        new_messages.append(user_msg)

        logger.info(
            "DynamicContextMiddleware[mem0]: %s turn — searched %d memories, "
            "replacing %d old reminders (user_id=%s)",
            "first" if is_first_turn else "subsequent",
            len(memories), len(old_reminder_ids), user_id,
        )

        return {
            "messages": new_messages,
            "memory_context": memories_text,
        }

    def _inject_file_legacy(
        self, state: HarnessState, messages: list,
        user_id: str, current_date: str, mem_cfg,
    ) -> dict | None:
        """file backend：保留原有首回合注入+冻结逻辑（向后兼容）。"""
        from harness.memory.prompt import format_memory_for_injection
        from harness.memory.updater import get_memory_data

        last_date = _last_injected_date(messages)

        if last_date is None:
            # First turn: inject full reminder
            first_idx = next(
                (i for i, m in enumerate(messages) if _is_user_injection_target(m)),
                None,
            )
            if first_idx is None:
                return None

            memory_context = ""
            memory_block = ""
            if mem_cfg.injection_enabled:
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

            full_reminder = (
                f"<system-reminder>\n"
                f"{memory_block}"
                f"<current_date>{current_date}</current_date>\n"
                f"</system-reminder>"
            )
            logger.info(
                "DynamicContextMiddleware[file]: injecting full reminder "
                "(has_memory=%s, user_id=%s)",
                bool(memory_context), user_id or "default",
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
            messages[last_human_idx], self._build_date_only_reminder(),
        )
        logger.info(
            "DynamicContextMiddleware[file]: midnight crossing — "
            "injected date update (user_id=%s)",
            user_id or "default",
        )
        return {"messages": [reminder_msg, user_msg]}

    @override
    async def abefore_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        return await self._inject(state)
