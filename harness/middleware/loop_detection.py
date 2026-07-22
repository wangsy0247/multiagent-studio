"""LoopDetectionMiddleware — detect and break repetitive tool-call loops.

Matches DeerFlow's design:

  - ``abefore_agent``: clear stale pending warnings from the same thread's
    other runs.
  - ``aafter_model``: detect repetitive tool-call patterns (hash-based +
    frequency-based), queue warnings or mark pending hard-stop.
  - ``awrap_model_call``: inject queued warnings into the next model request.
    For hard-stop runs, also overrides ``tools=[]`` so the LLM is forced to
    produce a final text summary instead of being silently truncated.
  - ``aafter_agent``: clean up pending warnings and hard-stop flags for the
    current thread/run.

Why warnings are injected at ``wrap_model_call`` instead of ``after_model``:
  In ``after_model`` the tools node hasn't run yet, so no matching
  ``ToolMessage`` exists. Any inserted message lands between the assistant's
  tool_calls and their responses, breaking OpenAI/Moonshot tool-call pairing.
  By deferring to ``wrap_model_call``, every prior ToolMessage is already
  present and the warning is appended at the end — pairing intact.

Hard-stop vs warning:
  Both are deferred to ``wrap_model_call``. The difference is that hard-stop
  also passes ``tools=[]`` to the model, removing every tool from the request.
  The LLM cannot call any tools and must produce a final text answer — the
  agent gets one last chance to summarize its work rather than being cut off
  mid-stream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from typing import Any, override

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_DEFAULT_WARN_THRESHOLD = 3
_DEFAULT_HARD_LIMIT = 5
_DEFAULT_WINDOW_SIZE = 20
_DEFAULT_MAX_TRACKED_THREADS = 100
_DEFAULT_TOOL_FREQ_WARN = 30
_DEFAULT_TOOL_FREQ_HARD_LIMIT = 50
_MAX_PENDING_WARNINGS_PER_RUN = 4

_WARNING_MSG = (
    "[LOOP DETECTED] You are repeating the same tool calls. "
    "Stop calling tools and produce your final answer now. "
    "If you cannot complete the task, summarize what you accomplished so far."
)

_TOOL_FREQ_WARNING_MSG = (
    "[LOOP DETECTED] You have called {tool_name} {count} times without "
    "producing a final answer. Stop calling tools and produce your final "
    "answer now. If you cannot complete the task, summarize what you "
    "accomplished so far."
)

_HARD_STOP_MSG = (
    "[FORCED STOP] Repeated tool calls exceeded the safety limit. "
    "Producing final answer with results collected so far."
)

_TOOL_FREQ_HARD_STOP_MSG = (
    "[FORCED STOP] Tool {tool_name} called {count} times — exceeded the "
    "per-tool safety limit. Producing final answer with results collected so far."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normalize_tool_call_args(raw_args: object) -> tuple[dict, str | None]:
    if isinstance(raw_args, dict):
        return raw_args, None
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, raw_args
        if isinstance(parsed, dict):
            return parsed, None
        return {}, json.dumps(parsed, sort_keys=True, default=str)
    if raw_args is None:
        return {}, None
    return {}, json.dumps(raw_args, sort_keys=True, default=str)


def _stable_tool_key(name: str, args: dict, fallback_key: str | None) -> str:
    """Derive a stable key from salient args."""
    if name == "read_file" and fallback_key is None:
        path = args.get("path") or ""
        start_line = args.get("start_line", 1)
        end_line = args.get("end_line", start_line)
        try:
            start_line = int(start_line)
        except (TypeError, ValueError):
            start_line = 1
        try:
            end_line = int(end_line)
        except (TypeError, ValueError):
            end_line = start_line
        bucket_size = 200
        bucket_start = (max(start_line, 1) - 1) // bucket_size
        bucket_end = (max(end_line, 1) - 1) // bucket_size
        return f"{path}:{bucket_start}-{bucket_end}"

    if name in {"write_file", "str_replace"}:
        if fallback_key is not None:
            return fallback_key
        return json.dumps(args, sort_keys=True, default=str)

    salient_fields = ("path", "url", "query", "command", "pattern", "glob", "cmd")
    stable_args = {f: args[f] for f in salient_fields if args.get(f) is not None}
    if stable_args:
        return json.dumps(stable_args, sort_keys=True, default=str)
    if fallback_key is not None:
        return fallback_key
    return json.dumps(args, sort_keys=True, default=str)


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    normalized: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args, fallback_key = _normalize_tool_call_args(tc.get("args", {}))
        key = _stable_tool_key(name, args, fallback_key)
        normalized.append(f"{name}:{key}")
    normalized.sort()
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# LoopDetectionMiddleware
# ---------------------------------------------------------------------------


class LoopDetectionMiddleware(HarnessAgentMiddleware):
    """Detect and break repetitive tool-call loops.

    Two detection layers:
      1. **Hash-based** — catches identical tool-call multisets
      2. **Frequency-based** — catches the same tool type called many times
         with varying arguments (e.g. ``read_file`` on 40 different files)

    Parameters
    ----------
    warn_threshold : int
        Number of identical tool-call sets before injecting a warning. Default: 7.
    hard_limit : int
        Number of identical tool-call sets before forcing a hard stop. Default: 10.
    tool_freq_warn : int
        Per-tool-type call count before injecting a warning (Layer 2).
        Default: 30.
    tool_freq_hard_limit : int
        Per-tool-type call count before forcing a hard stop (Layer 2).
        Default: 50.
    window_size : int
        Size of the sliding window for tracking calls. Default: 20.
    """

    name = "loop_detection"

    def __init__(
        self,
        config: dict | None = None,
        *,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        tool_freq_warn: int = _DEFAULT_TOOL_FREQ_WARN,
        tool_freq_hard_limit: int = _DEFAULT_TOOL_FREQ_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__(config)
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.tool_freq_warn = tool_freq_warn
        self.tool_freq_hard_limit = tool_freq_hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self._lock = threading.Lock()
        # (thread_id, run_id) → list of call hashes (sliding window, 按 run 隔离)
        self._history: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        # (thread_id, run_id) → set of warned hashes
        self._warned: dict[tuple[str, str], set[str]] = defaultdict(set)
        # (thread_id, run_id) → {tool_name: call_count}
        self._tool_freq: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # (thread_id, run_id) → set of tool names already warned (frequency)
        self._tool_freq_warned: dict[tuple[str, str], set[str]] = defaultdict(set)
        # (thread_id, run_id) → list of pending warning messages
        self._pending_warnings: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._pending_warning_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        # (thread_id, run_id) — 标记哪些 run 触发了 hard stop, 下次 LLM 调用时去工具化
        self._pending_hard_stops: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # thread / run identity
    # ------------------------------------------------------------------

    @staticmethod
    def _get_thread_id(runtime: Runtime) -> str:
        if runtime.context:
            return str(runtime.context.get("thread_id", "default"))
        return "default"

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        if runtime.context:
            return str(runtime.context.get("run_id", "default"))
        return "default"

    def _pending_key(self, runtime: Runtime) -> tuple[str, str]:
        return (self._get_thread_id(runtime), self._get_run_id(runtime))

    # ------------------------------------------------------------------
    # lock helpers
    # ------------------------------------------------------------------

    def _evict_if_needed_locked(self) -> None:
        while len(self._history) > self.max_tracked_threads:
            evicted_key, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_key, None)
            self._tool_freq.pop(evicted_key, None)
            self._tool_freq_warned.pop(evicted_key, None)
            self._pending_warnings.pop(evicted_key, None)
            self._pending_warning_order.pop(evicted_key, None)
            self._pending_hard_stops.discard(evicted_key)

    # ------------------------------------------------------------------
    # detection
    # ------------------------------------------------------------------

    def _detect(self, state: HarnessState, runtime: Runtime) -> tuple[str | None, bool]:
        """Return (warning_message | None, should_hard_stop)."""
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        key = self._pending_key(runtime)  # (thread_id, run_id)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            if key in self._history:
                self._history.move_to_end(key)
            else:
                self._history[key] = []
                self._evict_if_needed_locked()

            history = self._history[key]
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size:]

            # refresh warned set — drop hashes no longer in window
            warned_hashes = self._warned.get(key)
            if warned_hashes is not None:
                warned_hashes.intersection_update(history)
                if not warned_hashes:
                    self._warned.pop(key, None)

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            # --- Layer 1: hash-based (identical call sets) ---
            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop key=%s count=%d tools=%s",
                    key, count, tool_names,
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[key]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected key=%s count=%d tools=%s",
                        key, count, tool_names,
                    )
                    return _WARNING_MSG, False

            # --- Layer 2: per-tool-type frequency ---
            freq = self._tool_freq[key]
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                freq[name] += 1
                tc_count = freq[name]

                if tc_count >= self.tool_freq_hard_limit:
                    logger.error(
                        "Tool frequency hard limit key=%s tool=%s count=%d",
                        key, name, tc_count,
                    )
                    return _TOOL_FREQ_HARD_STOP_MSG.format(tool_name=name, count=tc_count), True

                if tc_count >= self.tool_freq_warn:
                    warned_freq = self._tool_freq_warned[key]
                    if name not in warned_freq:
                        warned_freq.add(name)
                        logger.warning(
                            "Tool frequency warning key=%s tool=%s count=%d",
                            key, name, tc_count,
                        )
                        return _TOOL_FREQ_WARNING_MSG.format(tool_name=name, count=tc_count), False

        return None, False

    # ------------------------------------------------------------------
    # pending warnings
    # ------------------------------------------------------------------

    def _queue_pending_warning(self, runtime: Runtime, warning: str) -> None:
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings[pending_key]
            if warning not in warnings:
                warnings.append(warning)
            if len(warnings) > _MAX_PENDING_WARNINGS_PER_RUN:
                del warnings[: len(warnings) - _MAX_PENDING_WARNINGS_PER_RUN]
            self._pending_warning_order[pending_key] = None
            self._pending_warning_order.move_to_end(pending_key)

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        pending_key = self._pending_key(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(pending_key, [])
            self._pending_warning_order.pop(pending_key, None)
        return warnings

    def _clear_other_run_pending_warnings(self, runtime: Runtime) -> None:
        """清理同一 thread 下其他 run 的残留状态 (异常中断残留)."""
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            for key in list(self._pending_warnings):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._pending_warnings.pop(key, None)
                    self._pending_warning_order.pop(key, None)
            for key in list(self._pending_hard_stops):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._pending_hard_stops.discard(key)
            for key in list(self._history):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._history.pop(key, None)
                    self._warned.pop(key, None)
                    self._tool_freq.pop(key, None)
                    self._tool_freq_warned.pop(key, None)

    def _clear_current_run_pending_warnings(self, runtime: Runtime) -> None:
        """清理当前 run 的所有状态 (pending warnings + 检测数据)."""
        key = self._pending_key(runtime)
        with self._lock:
            self._pending_warnings.pop(key, None)
            self._pending_warning_order.pop(key, None)
            self._pending_hard_stops.discard(key)
            # 检测状态按 run 隔离, run 结束时清理
            self._history.pop(key, None)
            self._warned.pop(key, None)
            self._tool_freq.pop(key, None)
            self._tool_freq_warned.pop(key, None)

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------

    @override
    async def abefore_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        self._clear_other_run_pending_warnings(runtime)
        return None

    @override
    async def aafter_model(self, state: HarnessState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._detect(state, runtime)

        if hard_stop:
            # 不再直接替换消息 — 改为延迟到下次 LLM 调用时:
            #   1. 注入警告消息 (和 warning 一样)
            #   2. override(tools=[]) 去工具化, 迫使 LLM 输出纯文本总结
            self._queue_pending_warning(runtime, warning or _HARD_STOP_MSG)
            with self._lock:
                self._pending_hard_stops.add(self._pending_key(runtime))
            return None

        if warning:
            self._queue_pending_warning(runtime, warning)

        return None

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Inject queued loop warnings / hard-stop messages into the next model request."""
        warnings = self._drain_pending_warnings(request.runtime)

        # 检查是否有 pending hard stop — 需要去工具化迫使 LLM 输出总结
        pending_key = self._pending_key(request.runtime)
        is_hard_stop = False
        with self._lock:
            if pending_key in self._pending_hard_stops:
                self._pending_hard_stops.discard(pending_key)
                is_hard_stop = True

        if warnings:
            deduped = list(dict.fromkeys(warnings))
            request = request.override(
                messages=[
                    *request.messages,
                    HumanMessage(
                        content="\n\n".join(deduped),
                        name="loop_warning",
                    ),
                ]
            )

        # hard stop → 清空 tools 列表, LLM 无法调用任何工具, 被迫输出纯文本总结
        if is_hard_stop:
            request = request.override(tools=[])

        return await handler(request)

    @override
    async def aafter_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    def reset(self, thread_id: str | None = None) -> None:
        """Clear tracking state. If *thread_id* given, clear only that thread."""
        with self._lock:
            if thread_id:
                for key in list(self._history):
                    if key[0] == thread_id:
                        self._history.pop(key, None)
                        self._warned.pop(key, None)
                        self._tool_freq.pop(key, None)
                        self._tool_freq_warned.pop(key, None)
                for key in list(self._pending_warnings):
                    if key[0] == thread_id:
                        self._pending_warnings.pop(key, None)
                        self._pending_warning_order.pop(key, None)
                for key in list(self._pending_hard_stops):
                    if key[0] == thread_id:
                        self._pending_hard_stops.discard(key)
            else:
                self._history.clear()
                self._warned.clear()
                self._tool_freq.clear()
                self._tool_freq_warned.clear()
                self._pending_warnings.clear()
                self._pending_warning_order.clear()
                self._pending_hard_stops.clear()
