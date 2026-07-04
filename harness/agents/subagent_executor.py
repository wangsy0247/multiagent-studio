"""SubAgentExecutor — isolated execution engine for delegated tasks.

Aligns with DeerFlow's ``SubagentExecutor``: runs subagents on a persistent,
isolated event loop in a daemon thread, decoupled from the parent HTTP request
lifecycle.  Supports cooperative cancellation, wall-clock timeout, streaming
via ``astream``, and lightweight token collection.

Key design decisions:
- Persistent daemon-thread event loop survives parent request cancellation.
- Cooperative cancellation via ``threading.Event`` checked at each ``astream``
  iteration boundary — never uses ``Future.cancel()``.
- ``SubagentTokenCollector`` callback replaces heavyweight TokenUsageMiddleware.
- ``astream(stream_mode="values")`` with incremental message detection collects
  ALL message types (AIMessage + ToolMessage), not just the last AIMessage.
- ``asyncio.Queue`` + ``run_coroutine_threadsafe`` bridges subagent messages
  from the isolated daemon thread to the main event loop for real-time SSE.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import uuid
from contextvars import Context, copy_context
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from harness.agents.subagent_middleware import build_subagent_middlewares
from harness.agents.subagent_token import SubagentTokenCollector
from harness.models import HarnessState, SubAgentConfig, SubAgentResult

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Persistent isolated event loop (module-level singleton)
# ══════════════════════════════════════════════════════════════════════════════

_isolated_loop: asyncio.AbstractEventLoop | None = None
_isolated_loop_thread: threading.Thread | None = None
_isolated_loop_lock = threading.Lock()

# Thread pool for background task scheduling
_scheduler_pool = ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="subagent-scheduler-"
)

# ══════════════════════════════════════════════════════════════════════════════
# Real-time subagent message streams (thread-safe bridge to main event loop)
# ══════════════════════════════════════════════════════════════════════════════

# Per-subagent asyncio.Queue consumed by the SSE handler on the main loop.
# Keyed by subagent name (e.g. "researcher", "coder").
# Writes happen via run_coroutine_threadsafe() from the isolated daemon thread.
_subagent_streams: dict[str, "asyncio.Queue[dict[str, Any]]"] = {}
_subagent_streams_lock = threading.Lock()


def get_subagent_stream(name: str) -> "asyncio.Queue[dict[str, Any]]":
    """Get or create the real-time message queue for a subagent.

    Called from the main event loop to obtain a consumer handle,
    and from the isolated daemon thread (via run_coroutine_threadsafe)
    to push messages.
    """
    with _subagent_streams_lock:
        if name not in _subagent_streams:
            _subagent_streams[name] = asyncio.Queue()
        return _subagent_streams[name]


def remove_subagent_stream(name: str) -> None:
    """Remove a subagent's message queue (called after execution completes)."""
    with _subagent_streams_lock:
        _subagent_streams.pop(name, None)


def list_active_subagent_names() -> list[str]:
    """Return names of subagents with active message streams."""
    with _subagent_streams_lock:
        return list(_subagent_streams.keys())


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """Convert a LangChain message to a plain dict, handling edge cases."""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    return {"content": str(msg), "type": getattr(msg, "type", "unknown")}


def _run_isolated_loop(
    loop: asyncio.AbstractEventLoop,
    started_event: threading.Event,
) -> None:
    """Run the persistent isolated subagent loop in a dedicated daemon thread."""
    asyncio.set_event_loop(loop)
    loop.call_soon(started_event.set)
    try:
        loop.run_forever()
    finally:
        started_event.clear()


def _shutdown_isolated_loop() -> None:
    """Stop and close the persistent isolated subagent loop (atexit callback)."""
    global _isolated_loop, _isolated_loop_thread

    with _isolated_loop_lock:
        loop = _isolated_loop
        thread = _isolated_loop_thread
        _isolated_loop = None
        _isolated_loop_thread = None

    if loop is None:
        return

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1)

    thread_stopped = thread is None or not thread.is_alive()
    if not loop.is_closed() and thread_stopped and not loop.is_running():
        loop.close()


atexit.register(_shutdown_isolated_loop)


def _get_isolated_loop() -> asyncio.AbstractEventLoop:
    """Return the persistent event loop used by isolated subagent executions."""
    global _isolated_loop, _isolated_loop_thread
    with _isolated_loop_lock:
        thread_alive = _isolated_loop_thread is not None and _isolated_loop_thread.is_alive()
        loop_usable = (
            _isolated_loop is not None
            and not _isolated_loop.is_closed()
            and _isolated_loop.is_running()
            and thread_alive
        )

        if not loop_usable:
            loop = asyncio.new_event_loop()
            started = threading.Event()
            thread = threading.Thread(
                target=_run_isolated_loop,
                args=(loop, started),
                name="subagent-isolated-loop",
                daemon=True,
            )
            thread.start()
            if not started.wait(timeout=5):
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
                loop.close()
                raise RuntimeError("Timed out starting isolated subagent event loop")
            _isolated_loop = loop
            _isolated_loop_thread = thread

        if _isolated_loop is None:
            raise RuntimeError("Isolated subagent event loop is not initialized")
        return _isolated_loop


def _submit_to_isolated_loop(
    context: Context,
    coro_factory,
) -> Future:
    """Submit a coroutine to the isolated loop while preserving ContextVar state."""
    return context.run(
        lambda: asyncio.run_coroutine_threadsafe(
            coro_factory(),
            _get_isolated_loop(),
        )
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tool filtering
# ══════════════════════════════════════════════════════════════════════════════


def _filter_tools(
    all_tools: list[BaseTool],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[BaseTool]:
    """Filter tools based on subagent configuration.

    Args:
        all_tools: List of all available tools.
        allowed: Optional allowlist of tool names.
        disallowed: Optional denylist of tool names.

    Returns:
        Filtered list of tools.
    """
    filtered = list(all_tools)

    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]

    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]

    return filtered


# ══════════════════════════════════════════════════════════════════════════════
# SubagentExecutor
# ══════════════════════════════════════════════════════════════════════════════


class SubagentExecutor:
    """Execute a SubAgent in an isolated event loop with timeout and cancellation.

    Parameters
    ----------
    config : SubAgentConfig
        SubAgent configuration (name, system_prompt, tools, max_turns, timeout).
    llm : BaseChatModel
        Pre-resolved LLM instance.
    tools : list[BaseTool]
        Full tool list (will be filtered by allowlist / denylist).
    parent_state : HarnessState | None
        Parent Lead Agent's state for sandbox / thread_data / thread_id inheritance.
    trace_id : str | None
        Trace ID for distributed tracing.  Auto-generated when omitted.
    """

    def __init__(
        self,
        config: SubAgentConfig,
        llm: BaseChatModel,
        tools: list[BaseTool],
        parent_state: HarnessState | None = None,
        trace_id: str | None = None,
    ):
        self.config = config
        self.llm = llm
        self.parent_state = parent_state or {}
        self.trace_id = trace_id or str(uuid.uuid4())[:8]

        # Filter tools
        self._base_tools = _filter_tools(
            tools,
            config.tools,
            config.disallowed_tools,
        )

        # Extract thread_id from parent state
        self.thread_id = self.parent_state.get("thread_id", "")

        # ── capture main event loop for cross-thread message delivery ──
        try:
            self._main_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            # No running loop (e.g. tests, scripts) → skip real-time streaming
            self._main_loop = None

        logger.info(
            "[trace=%s] SubagentExecutor initialized: name=%s tools=%d max_turns=%d timeout=%ds stream=%s",
            self.trace_id,
            config.name,
            len(self._base_tools),
            config.max_turns,
            config.timeout_seconds,
            "enabled" if self._main_loop else "disabled",
        )

    # ------------------------------------------------------------------
    # agent factory
    # ------------------------------------------------------------------

    def _create_agent(self, tools: list[BaseTool]):
        """Build the langgraph agent with stripped-down subagent middlewares."""
        middlewares = build_subagent_middlewares(
            vision_enabled=False,
            guardrail_enabled=False,
        )
        return create_agent(
            model=self.llm,
            tools=tools,
            system_prompt=None,  # injected via initial state messages
            middleware=middlewares,
            state_schema=HarnessState,
        )

    # ------------------------------------------------------------------
    # state building
    # ------------------------------------------------------------------

    def _build_initial_state(self, task: str) -> dict[str, Any]:
        """Build the initial state dict for SubAgent execution.

        Merges system_prompt + task into a single SystemMessage to avoid
        multi-SystemMessage rejection by some LLM APIs (e.g. Anthropic).
        Inherits sandbox + thread_data from parent state.
        """
        messages: list[Any] = []

        if self.config.system_prompt:
            messages.append(SystemMessage(content=self.config.system_prompt))

        messages.append(HumanMessage(content=task))

        state: dict[str, Any] = {"messages": messages}

        # ── inherit sandbox from parent to avoid creating a new sandbox ──
        sandbox = self.parent_state.get("sandbox")
        if sandbox is not None:
            state["sandbox"] = sandbox

        # ── inherit thread_data for path mapping consistency ──
        thread_data = self.parent_state.get("thread_data")
        if thread_data is not None:
            state["thread_data"] = thread_data

        # ── pass through thread_id / user_id for directory init ──
        thread_id = self.parent_state.get("thread_id")
        if thread_id:
            state["thread_id"] = thread_id
        user_id = self.parent_state.get("user_id")
        if user_id:
            state["user_id"] = user_id

        return state

    # ------------------------------------------------------------------
    # real-time stream push (cross-thread safe)
    # ------------------------------------------------------------------

    def _push_to_stream(self, msg_dict: dict[str, Any], iteration: int) -> None:
        """Push a subagent message to the main event loop's queue for SSE.

        Safe to call from any thread.  When ``_main_loop`` is None
        (no running loop at construction time), this is a no-op.
        """
        if self._main_loop is None or self._main_loop.is_closed():
            return
        try:
            stream = get_subagent_stream(self.config.name)
            asyncio.run_coroutine_threadsafe(
                stream.put({
                    "subagent_name": self.config.name,
                    "trace_id": self.trace_id,
                    "iteration": iteration,
                    "msg": msg_dict,
                }),
                self._main_loop,
            )
        except Exception:
            # Best-effort: never let a stream push failure crash the subagent
            logger.debug(
                "[trace=%s] Failed to push subagent message to stream",
                self.trace_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # async execution core
    # ------------------------------------------------------------------

    async def _aexecute(
        self,
        task: str,
        cancel_event: threading.Event,
        result_holder: SubAgentResult,
    ) -> SubAgentResult:
        """Async execution core — stream-based with cooperative cancellation.

        Parameters
        ----------
        task : str
            The task instruction for the SubAgent.
        cancel_event : threading.Event
            Set by the caller to request cooperative cancellation.
        result_holder : SubAgentResult
            Pre-created result object to populate during execution.
        """
        collector: SubagentTokenCollector | None = None
        try:
            state = self._build_initial_state(task)
            agent = self._create_agent(self._base_tools)

            collector = SubagentTokenCollector(
                caller=f"subagent:{self.config.name}"
            )

            run_config: RunnableConfig = {
                "recursion_limit": self.config.max_turns,
                "callbacks": [collector],
                "tags": [f"subagent:{self.config.name}"],
            }
            if self.thread_id:
                run_config["configurable"] = {"thread_id": self.thread_id}

            logger.info(
                "[trace=%s] SubAgent '%s' starting execution, max_turns=%d",
                self.trace_id,
                self.config.name,
                self.config.max_turns,
            )

            # Pre-check: bail if already cancelled
            if cancel_event.is_set():
                logger.info(
                    "[trace=%s] SubAgent '%s' cancelled before streaming",
                    self.trace_id,
                    self.config.name,
                )
                result_holder.status = "cancelled"
                result_holder.error = "Cancelled by user"
                result_holder.token_usage_records = collector.snapshot_records()
                return result_holder

            final_state = None
            all_messages: list[dict[str, Any]] = []
            last_msg_count = 0
            iteration = 0

            async for chunk in agent.astream(
                state,
                config=run_config,
                stream_mode="values",
            ):
                # Cooperative cancellation check at each iteration boundary
                if cancel_event.is_set():
                    logger.info(
                        "[trace=%s] SubAgent '%s' cancelled during streaming",
                        self.trace_id,
                        self.config.name,
                    )
                    result_holder.status = "cancelled"
                    result_holder.error = "Cancelled by user"
                    result_holder.token_usage_records = collector.snapshot_records()
                    return result_holder

                final_state = chunk
                iteration += 1

                # ── incremental message detection ──
                # stream_mode="values" returns full state snapshots.
                # New messages = current snapshot messages - previous snapshot messages.
                current_messages = chunk.get("messages", [])
                new_msgs = current_messages[last_msg_count:]

                for msg in new_msgs:
                    msg_dict = _msg_to_dict(msg)
                    all_messages.append(msg_dict)
                    # Push to real-time stream for SSE broadcasting
                    self._push_to_stream(msg_dict, iteration)

                last_msg_count = len(current_messages)

            logger.info(
                "[trace=%s] SubAgent '%s' execution completed — %d messages collected",
                self.trace_id,
                self.config.name,
                len(all_messages),
            )

            # ── push sentinel to signal completion and trigger cleanup ──
            self._push_to_stream({"__sentinel__": True}, iteration)

            # ── extract final result ──
            final_result = self._extract_final_message(final_state)
            token_records = collector.snapshot_records()
            iterations = sum(
                1 for m in all_messages if m.get("type") == "ai"
            )

            result_holder.status = "success"
            result_holder.output = final_result
            result_holder.iterations = iterations
            result_holder.ai_messages = all_messages  # 全部消息类型
            result_holder.token_usage_records = token_records
            result_holder.completed_at = datetime.now(timezone.utc).isoformat()

        except Exception as exc:
            logger.exception(
                "[trace=%s] SubAgent '%s' execution failed",
                self.trace_id,
                self.config.name,
            )
            result_holder.status = "error"
            result_holder.error = str(exc)
            result_holder.output = str(exc)
            result_holder.completed_at = datetime.now(timezone.utc).isoformat()
            if collector is not None:
                result_holder.token_usage_records = collector.snapshot_records()

        return result_holder

    # ------------------------------------------------------------------
    # result extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_final_message(final_state: dict | None) -> str:
        """Extract the last AIMessage content from the final state."""
        if final_state is None:
            return "No response generated"

        messages = final_state.get("messages", [])
        last_ai = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                last_ai = msg
                break

        if last_ai is not None:
            content = last_ai.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts) if parts else "No text content"
            return str(content)

        if messages:
            last_msg = messages[-1]
            raw = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                parts = []
                for block in raw:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts) if parts else "No text content"
            return str(raw)

        return "No response generated"

    # ------------------------------------------------------------------
    # public API: sync entry points
    # ------------------------------------------------------------------

    def execute(self, task: str) -> SubAgentResult:
        """Execute a task synchronously.

        When called from within an already-running event loop (e.g. the parent
        agent is async), this method routes execution onto the persistent
        isolated loop to avoid event-loop conflicts.  When called outside any
        event loop, ``asyncio.run()`` is used.

        Returns a completed ``SubAgentResult``.
        """
        trace_id = self.trace_id
        cancel_event = threading.Event()

        result = SubAgentResult(
            task_id=str(uuid.uuid4())[:8],
            trace_id=trace_id,
            status="success",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Detect whether we're already inside a running event loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None and running_loop.is_running():
                logger.debug(
                    "[trace=%s] SubAgent '%s' detected running loop — using isolated loop",
                    trace_id,
                    self.config.name,
                )
                return self._execute_in_isolated_loop(task, cancel_event, result)

            # No running loop — standard path
            return asyncio.run(self._aexecute(task, cancel_event, result))

        except Exception as exc:
            logger.exception(
                "[trace=%s] SubAgent '%s' sync execution failed",
                trace_id,
                self.config.name,
            )
            result.status = "error"
            result.error = str(exc)
            result.output = str(exc)
            return result

    def _execute_in_isolated_loop(
        self,
        task: str,
        cancel_event: threading.Event,
        result_holder: SubAgentResult,
    ) -> SubAgentResult:
        """Route execution onto the persistent isolated event loop.

        Blocks the calling thread until completion or timeout.
        """
        parent_context = copy_context()
        future: Future | None = None
        try:
            future = _submit_to_isolated_loop(
                parent_context,
                lambda: self._aexecute(task, cancel_event, result_holder),
            )
            return future.result(timeout=self.config.timeout_seconds)
        except FuturesTimeoutError:
            cancel_event.set()
            if future is not None:
                future.cancel()
            logger.error(
                "[trace=%s] SubAgent '%s' timed out after %ds",
                self.trace_id,
                self.config.name,
                self.config.timeout_seconds,
            )
            result_holder.status = "timed_out"
            result_holder.error = (
                f"Execution timed out after {self.config.timeout_seconds}s"
            )
            result_holder.completed_at = datetime.now(timezone.utc).isoformat()
            return result_holder

    # ------------------------------------------------------------------
    # public API: background execution
    # ------------------------------------------------------------------

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        """Start task execution in the background.

        Returns a task_id that can be used with ``get_background_result()``
        to poll for completion.

        Parameters
        ----------
        task : str
            The task instruction.
        task_id : str | None
            Optional task ID.  Auto-generated when omitted.

        Returns
        -------
        str
            Task ID for status polling.
        """
        if task_id is None:
            task_id = str(uuid.uuid4())[:8]

        cancel_event = threading.Event()
        result = SubAgentResult(
            task_id=task_id,
            trace_id=self.trace_id,
            status="success",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        with _background_tasks_lock:
            _background_tasks[task_id] = result

        parent_context = copy_context()

        def _run_background() -> None:
            try:
                execution_future = _submit_to_isolated_loop(
                    parent_context,
                    lambda: self._aexecute(task, cancel_event, result),
                )
                try:
                    execution_future.result(timeout=self.config.timeout_seconds)
                except FuturesTimeoutError:
                    cancel_event.set()
                    result.status = "timed_out"
                    result.error = (
                        f"Execution timed out after {self.config.timeout_seconds}s"
                    )
                    result.completed_at = datetime.now(timezone.utc).isoformat()
                    execution_future.cancel()
            except Exception as exc:
                logger.exception(
                    "[trace=%s] Background subagent '%s' failed",
                    self.trace_id,
                    self.config.name,
                )
                result.status = "error"
                result.error = str(exc)
                result.completed_at = datetime.now(timezone.utc).isoformat()

        _scheduler_pool.submit(_run_background)
        logger.info(
            "[trace=%s] Background subagent started: name=%s task_id=%s",
            self.trace_id,
            self.config.name,
            task_id,
        )
        return task_id


# ══════════════════════════════════════════════════════════════════════════════
# Background task registry (for execute_async + polling)
# ══════════════════════════════════════════════════════════════════════════════

_background_tasks: dict[str, SubAgentResult] = {}
_background_tasks_lock = threading.Lock()


def get_background_result(task_id: str) -> SubAgentResult | None:
    """Get the result of a background task by ID."""
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def cancel_background_task(task_id: str) -> None:
    """Request cancellation of a running background task.

    Does NOT force-kill; sets the cancel_event so the subagent stops at
    the next ``astream`` iteration boundary.
    """
    # Note: cooperative cancellation is implemented via cancel_event inside
    # SubagentExecutor._aexecute.  Background tasks created by execute_async
    # hold their cancel_event in the closure; external cancellation requires
    # storing the event alongside the result.  For now, this is a placeholder.
    logger.warning(
        "cancel_background_task(%s): cooperative cancellation not yet wired for background tasks",
        task_id,
    )


def cleanup_background_task(task_id: str) -> None:
    """Remove a completed background task from the registry."""
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is None:
            return
        terminal_statuses = {"success", "error", "cancelled", "timed_out", "max_iterations_reached"}
        if result.status in terminal_statuses or result.completed_at is not None:
            del _background_tasks[task_id]
            logger.debug("Cleaned up background task: %s", task_id)
