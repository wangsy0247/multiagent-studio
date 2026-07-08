"""LoopDetectionMiddleware — detect and break repetitive tool-call loops.

Matches DeerFlow's design:

  - ``abefore_agent``: clear stale pending warnings from the same thread's
    other runs.
  - ``aafter_model``: detect repetitive tool-call patterns (hash-based +
    frequency-based), queue warnings or force hard-stop.
  - ``awrap_model_call``: inject queued warnings into the next model request.
  - ``aafter_agent``: clean up pending warnings for the current thread/run.

Why warnings are injected at ``wrap_model_call`` instead of ``after_model``:
  In ``after_model`` the tools node hasn't run yet, so no matching
  ``ToolMessage`` exists. Any inserted message lands between the assistant's
  tool_calls and their responses, breaking OpenAI/Moonshot tool-call pairing.
  By deferring to ``wrap_model_call``, every prior ToolMessage is already
  present and the warning is appended at the end — pairing intact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from copy import deepcopy
from typing import Any, override

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

_DEFAULT_WARN_THRESHOLD = 7
_DEFAULT_HARD_LIMIT = 10
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
        Number of identical tool-call sets before injecting a warning. Default: 3.
    hard_limit : int
        Number of identical tool-call sets before forcing a hard stop. Default: 5.
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
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__(config)
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self._lock = threading.Lock()
        # thread_id → list of call hashes (sliding window)
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        # thread_id → set of warned hashes
        self._warned: dict[str, set[str]] = defaultdict(set)
        # thread_id → {tool_name: call_count}
        self._tool_freq: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # thread_id → set of tool names already warned (frequency)
        self._tool_freq_warned: dict[str, set[str]] = defaultdict(set)
        # (thread_id, run_id) → list of pending warning messages
        self._pending_warnings: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._pending_warning_order: OrderedDict[tuple[str, str], None] = OrderedDict()

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
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            self._tool_freq.pop(evicted_id, None)
            self._tool_freq_warned.pop(evicted_id, None)
            for key in list(self._pending_warnings):
                if key[0] == evicted_id:
                    self._pending_warnings.pop(key, None)
                    self._pending_warning_order.pop(key, None)

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

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed_locked()

            history = self._history[thread_id]
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size:]

            # refresh warned set — drop hashes no longer in window
            warned_hashes = self._warned.get(thread_id)
            if warned_hashes is not None:
                warned_hashes.intersection_update(history)
                if not warned_hashes:
                    self._warned.pop(thread_id, None)

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            # --- Layer 1: hash-based (identical call sets) ---
            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop thread=%s count=%d tools=%s",
                    thread_id, count, tool_names,
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected thread=%s count=%d tools=%s",
                        thread_id, count, tool_names,
                    )
                    return _WARNING_MSG, False

            # --- Layer 2: per-tool-type frequency ---
            freq = self._tool_freq[thread_id]
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                freq[name] += 1
                tc_count = freq[name]

                if tc_count >= self.hard_limit:
                    logger.error(
                        "Tool frequency hard limit thread=%s tool=%s count=%d",
                        thread_id, name, tc_count,
                    )
                    return _TOOL_FREQ_HARD_STOP_MSG.format(tool_name=name, count=tc_count), True

                if tc_count >= self.warn_threshold:
                    warned_freq = self._tool_freq_warned[thread_id]
                    if name not in warned_freq:
                        warned_freq.add(name)
                        logger.warning(
                            "Tool frequency warning thread=%s tool=%s count=%d",
                            thread_id, name, tc_count,
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
        thread_id, current_run_id = self._pending_key(runtime)
        with self._lock:
            for key in list(self._pending_warnings):
                if key[0] == thread_id and key[1] != current_run_id:
                    self._pending_warnings.pop(key, None)
                    self._pending_warning_order.pop(key, None)

    def _clear_current_run_pending_warnings(self, runtime: Runtime) -> None:
        pending_key = self._pending_key(runtime)
        with self._lock:
            self._pending_warnings.pop(pending_key, None)
            self._pending_warning_order.pop(pending_key, None)

    # ------------------------------------------------------------------
    # hard stop
    # ------------------------------------------------------------------

    @staticmethod
    def _build_hard_stop_update(last_msg: AIMessage, warning: str) -> dict:
        new_content = (last_msg.content or "") + f"\n\n{warning}"
        update: dict = {"tool_calls": [], "content": new_content}
        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "function_call"):
            additional_kwargs.pop(key, None)
        update["additional_kwargs"] = additional_kwargs
        response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"
        update["response_metadata"] = response_metadata
        return update

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
            messages = state.get("messages", [])
            last_msg = messages[-1]
            stripped = last_msg.model_copy(
                update=self._build_hard_stop_update(last_msg, warning or _HARD_STOP_MSG)
            )
            return {"messages": [stripped]}

        if warning:
            self._queue_pending_warning(runtime, warning)

        return None

    @override
    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Inject queued loop warnings into the next model request's messages."""
        warnings = self._drain_pending_warnings(request.runtime)
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
        return await handler(request)

    @override
    async def aafter_agent(self, state: HarnessState, runtime: Runtime) -> dict | None:
        self._clear_current_run_pending_warnings(runtime)
        return None

    def reset(self, thread_id: str | None = None) -> None:
        """Clear tracking state. If *thread_id* given, clear only that thread."""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
                self._tool_freq.pop(thread_id, None)
                self._tool_freq_warned.pop(thread_id, None)
                for key in list(self._pending_warnings):
                    if key[0] == thread_id:
                        self._pending_warnings.pop(key, None)
                        self._pending_warning_order.pop(key, None)
            else:
                self._history.clear()
                self._warned.clear()
                self._tool_freq.clear()
                self._tool_freq_warned.clear()
                self._pending_warnings.clear()
                self._pending_warning_order.clear()
