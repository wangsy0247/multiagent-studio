"""SummarizationMiddleware — extends LangChain's SummarizationMiddleware with hook support
and skill rescue.

Adapted from the reference implementation: adds ``before_summarization`` hooks so that
``memory_flush_hook`` can save conversation context to memory before
messages are compressed away, and rescues recently-loaded skill file
reads (AIMessage + ToolMessage bundles under ``/mnt/skills``) from
compression so the agent does not "forget" skill instructions it already
loaded.

Note: LangChain 1.3.x inlines the whole summarization flow into
``before_model`` / ``abefore_model`` — it never calls a ``_maybe_summarize``
helper. This middleware therefore overrides the entry hooks directly and
runs the full flow itself instead of delegating to ``super()``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol, override, runtime_checkable

from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware as LangChainSummarizationMiddleware
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.messages.utils import get_buffer_string
from langgraph.config import get_config
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from harness.config.paths import VIRTUAL_SKILLS_PATH
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


def _tool_call_path(tool_call: dict[str, Any]) -> str | None:
    """Best-effort extraction of a file path argument from a read_file-like tool call."""
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "filepath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _raw_tool_call_id(raw_tool_call: Any) -> str | None:
    if not isinstance(raw_tool_call, dict):
        return None
    raw_id = raw_tool_call.get("id")
    return raw_id if isinstance(raw_id, str) and raw_id else None


def _clone_ai_message(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any | None = None,
) -> AIMessage:
    """Clone an AIMessage while keeping raw provider tool-call metadata in sync.

    Ported from the reference ``tool_call_metadata.clone_ai_message_with_tool_calls``.
    """
    kept_ids = {tc["id"] for tc in tool_calls if isinstance(tc.get("id"), str) and tc["id"]}

    update: dict[str, Any] = {"tool_calls": tool_calls}
    if content is not None:
        update["content"] = content

    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        synced_raw_tool_calls = [
            raw_tc for raw_tc in raw_tool_calls if _raw_tool_call_id(raw_tc) in kept_ids
        ]
        if synced_raw_tool_calls:
            additional_kwargs["tool_calls"] = synced_raw_tool_calls
        else:
            additional_kwargs.pop("tool_calls", None)

    if not tool_calls:
        additional_kwargs.pop("function_call", None)

    update["additional_kwargs"] = additional_kwargs

    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    if not tool_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata

    return message.model_copy(update=update)


@dataclass
class _SkillBundle:
    """Skill-related tool calls and tool results associated with one AIMessage."""

    ai_index: int
    skill_tool_indices: tuple[int, ...]
    skill_tool_call_ids: frozenset[str]
    skill_tool_tokens: int
    skill_key: str


# ── harness-aligned SummarizationMiddleware ──────────────────────────────

class SummarizationMiddleware(LangChainSummarizationMiddleware):
    """LangChain SummarizationMiddleware extended with pre-summarization hooks
    and skill rescue.

    Hooks (e.g. ``memory_flush_hook``) are invoked just before messages
    are compressed, giving them a chance to persist important information.
    Skill rescue keeps recently-loaded skill file reads (under
    ``skills_container_path``) out of compression within count/token budgets.

    Accepts the same configuration format as the harness format via
    ``SummarizationConfig``.
    """

    name = "summarization"

    def __init__(
        self,
        *args,
        before_summarization: list[BeforeSummarizationHook] | None = None,
        preserve_dynamic_context_reminders: bool = True,
        skills_container_path: str | None = None,
        skill_file_read_tool_names: Collection[str] | None = None,
        preserve_recent_skill_count: int = 5,
        preserve_recent_skill_tokens: int = 25_000,
        preserve_recent_skill_tokens_per_skill: int = 5_000,
        **kwargs,
    ) -> None:
        """Initialize the summarization middleware.

        Args:
            *args: Passed through to LangChain's SummarizationMiddleware.
            before_summarization: Optional list of hook callables invoked
                before each summarization cycle.
            preserve_dynamic_context_reminders: If True, keep hidden
                dynamic-context reminders out of summary compression.
            skills_container_path: Container path prefix identifying skill
                file reads (default: ``/mnt/skills``).
            skill_file_read_tool_names: Tool names treated as skill file
                reads (default: file_read/read_file/read/view/cat).
            preserve_recent_skill_count: Max skill bundles to rescue (0 = off).
            preserve_recent_skill_tokens: Total token budget for rescued
                skill ToolMessages.
            preserve_recent_skill_tokens_per_skill: Per-skill token budget;
                larger skill loads are not rescued.
            **kwargs: Passed through to LangChain's SummarizationMiddleware.
        """
        # summary_prompt=None 会覆盖父类默认值, 导致 _create_summary 里
        # None.format() 报错 — 显式移除, 让父类回退到 DEFAULT_SUMMARY_PROMPT.
        if "summary_prompt" in kwargs and kwargs["summary_prompt"] is None:
            del kwargs["summary_prompt"]
        super().__init__(*args, **kwargs)
        self._before_summarization_hooks = before_summarization or []
        self._preserve_dynamic_context_reminders_enabled = preserve_dynamic_context_reminders
        self._skills_container_path = skills_container_path or VIRTUAL_SKILLS_PATH
        self._skill_file_read_tool_names = frozenset(
            skill_file_read_tool_names or {"file_read", "read_file", "read", "view", "cat"}
        )
        self._preserve_recent_skill_count = max(0, preserve_recent_skill_count)
        self._preserve_recent_skill_tokens = max(0, preserve_recent_skill_tokens)
        self._preserve_recent_skill_tokens_per_skill = max(
            0, preserve_recent_skill_tokens_per_skill
        )

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._maybe_summarize(state, runtime)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return await self._amaybe_summarize(state, runtime)

    @override
    def _build_new_messages(self, summary: str) -> list[HumanMessage]:
        """Summary message hidden from UI but visible to the model."""
        return [
            HumanMessage(
                content=f"Here is a summary of the conversation to date:\n\n{summary}",
                name="summary",
            )
        ]

    @staticmethod
    def _summary_call_config() -> dict:
        """摘要 LLM 调用的 config — 继承当前运行的 callbacks.

        父类用 ``config={"metadata": ...}`` 直接调裸模型, 不带 LangChain
        回调, 导致 Langfuse (CallbackHandler 走 RunnableConfig.callbacks)
        追踪不到摘要调用。这里从 langgraph 的运行时 config 中取回 callbacks.
        """
        config: dict = {"metadata": {"lc_source": "summarization"}}
        try:
            rc = get_config()
            callbacks = rc.get("callbacks") if rc else None
            if callbacks:
                config["callbacks"] = callbacks
        except Exception:
            pass
        return config

    @override
    def _create_summary(self, messages_to_summarize: list) -> str:
        """同父类逻辑, 但 config 继承运行 callbacks (Langfuse 可见) 且异常写日志."""
        if not messages_to_summarize:
            return "No previous conversation history."
        trimmed = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed:
            return "Previous conversation was too long to summarize."
        formatted = get_buffer_string(trimmed, format="xml")
        try:
            response = self.model.invoke(
                self.summary_prompt.format(messages=formatted).rstrip(),
                config=self._summary_call_config(),
            )
            return response.text.strip()
        except Exception as e:
            logger.exception("Summary generation failed")
            return f"Error generating summary: {e!s}"

    @override
    async def _acreate_summary(self, messages_to_summarize: list) -> str:
        """Async variant — 同 _create_summary."""
        if not messages_to_summarize:
            return "No previous conversation history."
        trimmed = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed:
            return "Previous conversation was too long to summarize."
        formatted = get_buffer_string(trimmed, format="xml")
        try:
            response = await self.model.ainvoke(
                self.summary_prompt.format(messages=formatted).rstrip(),
                config=self._summary_call_config(),
            )
            return response.text.strip()
        except Exception as e:
            logger.exception("Summary generation failed")
            return f"Error generating summary: {e!s}"

    def _maybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Full summarization flow with skill rescue and pre-compression hooks.

        Does not delegate to ``super()`` — LangChain 1.3.x has no
        ``_maybe_summarize`` on the parent; the parent inlines this flow in
        ``before_model``.
        """
        messages = list(state["messages"])
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_with_skill_rescue(
            messages, cutoff_index
        )
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(
            messages_to_summarize, preserved_messages
        )
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)

        logger.info(
            "Summarization started: msgs=%d tokens≈%d, compressing=%d preserving=%d",
            len(messages), total_tokens,
            len(messages_to_summarize), len(preserved_messages),
        )
        _t0 = time.monotonic()
        summary = self._create_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)
        logger.info(
            "Summarization done in %.1fs: %d msgs -> summary + %d preserved",
            time.monotonic() - _t0, len(messages), len(preserved_messages),
        )

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }

    async def _amaybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        """Async variant of ``_maybe_summarize``."""
        messages = list(state["messages"])
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_with_skill_rescue(
            messages, cutoff_index
        )
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(
            messages_to_summarize, preserved_messages
        )
        self._fire_hooks(messages_to_summarize, preserved_messages, runtime)

        logger.info(
            "Summarization started: msgs=%d tokens≈%d, compressing=%d preserving=%d",
            len(messages), total_tokens,
            len(messages_to_summarize), len(preserved_messages),
        )
        _t0 = time.monotonic()
        summary = await self._acreate_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)
        logger.info(
            "Summarization done in %.1fs: %d msgs -> summary + %d preserved",
            time.monotonic() - _t0, len(messages), len(preserved_messages),
        )

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }

    async def aforce_summarize(self, messages: list) -> dict | None:
        """手动触发压缩 (如 /compact 指令): 跳过 _should_summarize 阈值判断.

        复用与 ``_amaybe_summarize`` 相同的管线 (skill rescue + 动态上下文
        提醒保留 + 独立摘要模型), 但不 fire before_summarization hooks。

        Args:
            messages: 当前完整消息历史 (不需要 AgentState / Runtime).

        Returns:
            可传给 ``graph.aupdate_state`` 的 state 更新; 历史太短
            (cutoff_index <= 0, 即压缩后没有可保留的尾部) 时返回 None.
        """
        messages = list(messages)
        self._ensure_message_ids(messages)

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        messages_to_summarize, preserved_messages = self._partition_with_skill_rescue(
            messages, cutoff_index
        )
        messages_to_summarize, preserved_messages = self._preserve_dynamic_context_reminders(
            messages_to_summarize, preserved_messages
        )

        logger.info(
            "Manual summarization (/compact) started: msgs=%d, compressing=%d preserving=%d",
            len(messages), len(messages_to_summarize), len(preserved_messages),
        )
        _t0 = time.monotonic()
        summary = await self._acreate_summary(messages_to_summarize)
        new_messages = self._build_new_messages(summary)
        logger.info(
            "Manual summarization (/compact) done in %.1fs: %d msgs -> summary + %d preserved",
            time.monotonic() - _t0, len(messages), len(preserved_messages),
        )

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
                *preserved_messages,
            ]
        }

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

    # ── Skill rescue ─────────────────────────────────────────────────────

    def _partition_with_skill_rescue(
        self,
        messages: list[AnyMessage],
        cutoff_index: int,
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """Partition like the parent, then rescue recently-loaded skill bundles."""
        to_summarize, preserved = self._partition_messages(messages, cutoff_index)

        if (
            self._preserve_recent_skill_count == 0
            or self._preserve_recent_skill_tokens == 0
            or not to_summarize
        ):
            return to_summarize, preserved

        try:
            bundles = self._find_skill_bundles(to_summarize, self._skills_container_path)
        except Exception:
            logger.exception(
                "Skill-preserving summarization rescue failed; falling back to default partition"
            )
            return to_summarize, preserved

        if not bundles:
            return to_summarize, preserved

        rescue_bundles = self._select_bundles_to_rescue(bundles)
        if not rescue_bundles:
            return to_summarize, preserved

        bundles_by_ai_index = {bundle.ai_index: bundle for bundle in rescue_bundles}
        rescue_tool_indices = {
            idx for bundle in rescue_bundles for idx in bundle.skill_tool_indices
        }
        rescued: list[AnyMessage] = []
        remaining: list[AnyMessage] = []
        for i, msg in enumerate(to_summarize):
            bundle = bundles_by_ai_index.get(i)
            if bundle is not None and isinstance(msg, AIMessage):
                rescued_tool_calls = [
                    tc for tc in msg.tool_calls if tc.get("id") in bundle.skill_tool_call_ids
                ]
                remaining_tool_calls = [
                    tc for tc in msg.tool_calls if tc.get("id") not in bundle.skill_tool_call_ids
                ]

                if rescued_tool_calls:
                    rescued.append(_clone_ai_message(msg, rescued_tool_calls, content=""))
                if remaining_tool_calls or msg.content:
                    remaining.append(_clone_ai_message(msg, remaining_tool_calls))
                continue

            if i in rescue_tool_indices:
                rescued.append(msg)
                continue

            remaining.append(msg)

        return remaining, rescued + preserved

    def _find_skill_bundles(
        self,
        messages: list[AnyMessage],
        skills_root: str,
    ) -> list[_SkillBundle]:
        """Locate AIMessage + paired ToolMessage groups that load skill files."""
        bundles: list[_SkillBundle] = []
        n = len(messages)
        i = 0
        while i < n:
            msg = messages[i]
            if not (isinstance(msg, AIMessage) and msg.tool_calls):
                i += 1
                continue

            tool_calls = list(msg.tool_calls)
            skill_paths_by_id: dict[str, str] = {}
            for tc in tool_calls:
                if self._is_skill_tool_call(tc, skills_root):
                    tc_id = tc.get("id")
                    path = _tool_call_path(tc)
                    if tc_id and path:
                        skill_paths_by_id[tc_id] = path

            if not skill_paths_by_id:
                i += 1
                continue

            skill_tool_tokens = 0
            skill_key_parts: list[str] = []
            skill_tool_indices: list[int] = []
            matched_skill_call_ids: set[str] = set()

            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                j += 1

            for k in range(i + 1, j):
                tool_msg = messages[k]
                if isinstance(tool_msg, ToolMessage) and tool_msg.tool_call_id in skill_paths_by_id:
                    skill_tool_tokens += self.token_counter([tool_msg])
                    skill_key_parts.append(skill_paths_by_id[tool_msg.tool_call_id])
                    skill_tool_indices.append(k)
                    matched_skill_call_ids.add(tool_msg.tool_call_id)

            if not skill_tool_indices:
                i = j
                continue

            bundles.append(
                _SkillBundle(
                    ai_index=i,
                    skill_tool_indices=tuple(skill_tool_indices),
                    skill_tool_call_ids=frozenset(matched_skill_call_ids),
                    skill_tool_tokens=skill_tool_tokens,
                    skill_key="|".join(sorted(skill_key_parts)),
                )
            )
            i = j

        return bundles

    def _select_bundles_to_rescue(self, bundles: list[_SkillBundle]) -> list[_SkillBundle]:
        """Pick bundles to keep, walking newest-first under count/token budgets."""
        selected: list[_SkillBundle] = []
        if not bundles:
            return selected

        seen_skill_keys: set[str] = set()
        total_tokens = 0
        kept = 0

        for bundle in reversed(bundles):
            if kept >= self._preserve_recent_skill_count:
                break
            if bundle.skill_key in seen_skill_keys:
                continue
            if bundle.skill_tool_tokens > self._preserve_recent_skill_tokens_per_skill:
                continue
            if total_tokens + bundle.skill_tool_tokens > self._preserve_recent_skill_tokens:
                continue

            selected.append(bundle)
            total_tokens += bundle.skill_tool_tokens
            kept += 1
            seen_skill_keys.add(bundle.skill_key)

        selected.reverse()
        return selected

    def _is_skill_tool_call(self, tool_call: dict[str, Any], skills_root: str) -> bool:
        """Return True when ``tool_call`` reads a file under the configured skills root."""
        name = tool_call.get("name") or ""
        if name not in self._skill_file_read_tool_names:
            return False
        path = _tool_call_path(tool_call)
        if not path:
            return False
        normalized_root = skills_root.rstrip("/")
        return path == normalized_root or path.startswith(normalized_root + "/")

    # ── Hooks ────────────────────────────────────────────────────────────

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


# ── Factory ──────────────────────────────────────────────────────────────

def create_summarization_middleware(
    *,
    before_summarization: list[BeforeSummarizationHook] | None = None,
    model_name: str = "",
    api_key: str = "",
    base_url: str = "",
    user_id: str = "",
) -> SummarizationMiddleware | None:
    """Build a SummarizationMiddleware from SummarizationConfig.

    Args:
        before_summarization: Hooks invoked before each summarization cycle.
        model_name: Model name override (from EffectiveConfig).
        api_key: API key (from EffectiveConfig). Falls back to L1 config → env.
        base_url: Base URL (from EffectiveConfig). Falls back to L1 config → env.
        user_id: User ID for loading L1 config as fallback.
    """
    import os as _os

    cfg = get_summarization_config()
    if not cfg.enabled:
        return None

    # Resolve model name: explicit arg > config.yaml > SYSTEM_DEFAULTS
    effective_model = model_name or cfg.model_name
    if not effective_model:
        from harness.config.defaults import SYSTEM_DEFAULTS
        effective_model = SYSTEM_DEFAULTS.get("summary_model", "")
    if not effective_model:
        effective_model = _os.getenv("DEFAULT_MODEL", "gpt-4o")

    # ── 动态解析 api_key / base_url ──
    # 优先级: 显式传入 > 用户 L1 配置 > 环境变量
    effective_api_key = api_key
    effective_base_url = base_url
    if (not effective_api_key or not effective_base_url) and user_id:
        l1 = _load_user_config(user_id)
        if not effective_api_key:
            effective_api_key = l1.get("api_key", "")
        if not effective_base_url:
            effective_base_url = l1.get("base_url", "")
    effective_api_key = effective_api_key or _os.getenv("OPENAI_API_KEY", "")
    effective_base_url = effective_base_url or _os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    model = None
    if effective_model:
        try:
            from langchain_openai import ChatOpenAI
            model = ChatOpenAI(
                model=effective_model,
                api_key=effective_api_key,
                base_url=effective_base_url,
                temperature=0,
                request_timeout=60,
                max_retries=1,
            )
            logger.info(
                "Summarization LLM: model=%s",
                effective_model,
            )
        except Exception as exc:
            logger.warning("Failed to create summarization model: %s", exc)
            return None
    else:
        logger.warning("No summarization model configured, disabling summarization")
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
        model=model,
        before_summarization=before_summarization,
        preserve_dynamic_context_reminders=cfg.preserve_dynamic_context_reminders,
        skills_container_path=VIRTUAL_SKILLS_PATH,
        skill_file_read_tool_names=cfg.skill_file_read_tool_names,
        preserve_recent_skill_count=cfg.preserve_recent_skill_count,
        preserve_recent_skill_tokens=cfg.preserve_recent_skill_tokens,
        preserve_recent_skill_tokens_per_skill=cfg.preserve_recent_skill_tokens_per_skill,
    )


def _load_user_config(user_id: str) -> dict:
    """加载用户 L1 全局配置 (用于运行时动态解析 api_key/base_url)."""
    try:
        from harness.config.config_loader import ConfigLoader
        cfg = ConfigLoader.load_user_global(user_id)
        return cfg or {}
    except Exception:
        return {}
