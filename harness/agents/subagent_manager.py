"""SubAgent Manager — lifecycle, registry, concurrency, and isolated execution.

harness-aligned refactor:
- Uses ``SubagentExecutor`` for isolated event-loop execution.
- Concurrency gating via ``asyncio.Semaphore``.
- SubAgents receive a stripped-down middleware chain (7-9 items)
  via ``build_subagent_middlewares()`` instead of the full 20-middleware list.
- Supports synchronous ``execute()`` and background ``execute_async()``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from harness.agents.subagent_executor import (
    SubagentExecutor,
    cancel_background_task,
    cleanup_background_task,
    get_background_result,
)
from harness.models import HarnessState, SubAgentConfig, SubAgentResult, SubagentStatus
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _resolve_tools(
    config: SubAgentConfig,
    tool_registry: ToolRegistry,
    all_core_tools: list[BaseTool],
) -> list[BaseTool]:
    """Resolve the tool list for a SubAgent config.

    - If ``config.tools`` is None, all registry tools are inherited.
    - Otherwise, only explicitly listed tools are included.
    """
    if config.tools is None:
        return list(all_core_tools)

    return [
        tool_registry.get_tool(name)
        for name in config.tools
        if tool_registry.has_tool(name)
    ]


class SubagentManager:
    """Manage SubAgent creation, lookup, execution, and teardown.

    Concurrency is controlled via an ``asyncio.Semaphore`` whose size is
    clamped to [2, 4].  Execution is routed through ``SubagentExecutor``
    which runs on a persistent isolated event loop in a daemon thread.
    """

    def __init__(
        self,
        llm_factory: Callable[[str | None], BaseChatModel],
        tool_registry: ToolRegistry,
        max_concurrent: int = 3,
        *,
        skill_storage: Any | None = None,
        worktree_config: Any | None = None,  # WorktreeConfig | None
    ):
        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._max_concurrent: int = min(max(int(max_concurrent), 2), 4)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._skill_storage = skill_storage
        self._worktree_config = worktree_config
        self._worktree_mgr: Any = None  # GitWorktreeManager | None

        # Cache core tools at init time (they don't change per request)
        self._core_tools: list[BaseTool] = tool_registry.get_core_tools()

        # Registry: name → (config, tools, llm)
        self._agents: dict[
            str, tuple[SubAgentConfig, list[BaseTool], BaseChatModel]
        ] = {}

        # ── Last-result cache (for SSE handler retrieval) ──
        # Keyed by subagent name; consumed by main.py's on_tool_end
        # to build the subagent_end SSE event without exposing internal
        # details (ai_messages) to the Lead Agent's tool result.
        self._last_results: dict[str, SubAgentResult] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        config: SubAgentConfig,
        parent_model: str | None = None,
    ) -> SubagentExecutor:
        """Create and register a new SubAgent.

        Returns the ``SubagentExecutor`` so callers can inspect the resolved
        configuration.  Raises ``ValueError`` if the name already exists.
        """
        if config.name in self._agents:
            raise ValueError(f"SubAgent '{config.name}' 已存在")

        # Resolve model
        model_name: str | None = config.model
        if model_name == "inherit":
            # None → llm_factory uses its internal default (config.default_model)
            model_name = parent_model or None
        llm = self._llm_factory(model_name)

        # Resolve tools
        tools = _resolve_tools(config, self._tool_registry, self._core_tools)

        self._agents[config.name] = (config, tools, llm)
        logger.info(
            "SubAgent created: name=%s display=%s tools=%d model=%s timeout=%ds",
            config.name,
            config.display_name,
            len(tools),
            model_name or "default",
            config.timeout_seconds,
        )
        return SubagentExecutor(config=config, llm=llm, tools=tools)

    def get(self, name: str) -> SubAgentConfig | None:
        """Look up a SubAgent config by name."""
        entry = self._agents.get(name)
        return entry[0] if entry else None

    def list(self) -> list[SubAgentConfig]:
        """Return configs for all registered SubAgents."""
        return [entry[0] for entry in self._agents.values()]

    async def delete(self, name: str) -> None:
        """Remove a SubAgent."""
        self._agents.pop(name, None)

    def pop_last_result(self, name: str) -> SubAgentResult | None:
        """Retrieve and consume the last execution result for *name*.

        Used by the SSE handler (main.py) to build the ``subagent_end``
        event with full metadata without exposing internal details
        (ai_messages) to the Lead Agent's tool result text.
        """
        return self._last_results.pop(name, None)

    # ------------------------------------------------------------------
    # execution — synchronous (waits for completion)
    # ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        instruction: str,
        context: str = "",
        parent_state: HarnessState | None = None,
        *,
        parent_skills: list[str] | None = None,
    ) -> SubAgentResult:
        """Dispatch a task to a SubAgent and wait for completion.

        Concurrency is gated by ``asyncio.Semaphore``.  Execution runs on
        the persistent isolated event loop, decoupled from the parent
        HTTP request lifecycle.

        When the subagent config has ``isolation: worktree`` and worktree is
        enabled, a git worktree is created before execution and merged / cleaned
        up afterwards.
        """
        entry = self._agents.get(name)
        if entry is None:
            return SubAgentResult(
                status=SubagentStatus.ERROR,
                output=f"SubAgent '{name}' 不存在",
            )

        config, tools, llm = entry

        # Build instruction with optional context
        full_instruction = instruction
        if context:
            full_instruction = f"[上下文]\n{context}\n\n[任务]\n{instruction}"

        # ── Worktree isolation ──
        worktree_ctx = None
        _worktree_enabled = (
            config.isolation == "worktree"
            and self._worktree_config is not None
            and getattr(self._worktree_config, "enabled", False)
        )
        if _worktree_enabled:
            # Lazily initialise the worktree manager on first use.
            if self._worktree_mgr is None:
                # Resolve the workspace path from config.
                from harness.config.paths import get_paths
                workspace = str(get_paths().sandbox_work_dir("default"))
                # Override thread_id when parent_state provides it.
                if parent_state and parent_state.get("thread_id"):
                    tid = parent_state["thread_id"]
                    workspace = str(get_paths().sandbox_work_dir(tid))
                from harness.worktree.manager import GitWorktreeManager
                self._worktree_mgr = GitWorktreeManager(
                    workspace, self._worktree_config,
                )
                await self._worktree_mgr.ensure_git_repo()
            try:
                worktree_ctx = await self._worktree_mgr.create(name)
            except Exception as exc:
                logger.error(
                    "Failed to create worktree for '%s': %s — falling back "
                    "to shared workspace", name, exc,
                )

        try:
            async with self._semaphore:
                executor = SubagentExecutor(
                    config=config,
                    llm=llm,
                    tools=tools,
                    parent_state=parent_state,
                    skill_storage=self._skill_storage,
                    parent_skills=parent_skills,
                    worktree_ctx=worktree_ctx,
                )
                try:
                    # execute() is blocking on the isolated loop — run in thread
                    result = await asyncio.to_thread(executor.execute, full_instruction)
                    # ── Cache the full result for the SSE handler ──
                    self._last_results[name] = result
                    return result
                except asyncio.CancelledError:
                    logger.info(
                        "SubAgent '%s' cancelled by parent — signalling stop",
                        name,
                    )
                    executor.request_cancel()
                    try:
                        await asyncio.shield(asyncio.sleep(1))
                    except asyncio.CancelledError:
                        pass
                    raise
        finally:
            # ── Worktree merge & cleanup ──
            if worktree_ctx is not None and self._worktree_mgr is not None:
                try:
                    merge_result = await self._worktree_mgr.merge(worktree_ctx)
                    logger.info(
                        "Worktree merge for '%s': status=%s files=%d",
                        name, merge_result.status, merge_result.files_changed,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to merge worktree for '%s': %s", name, exc,
                    )
                try:
                    await self._worktree_mgr.cleanup(worktree_ctx)
                except Exception as exc:
                    logger.error(
                        "Failed to cleanup worktree for '%s': %s", name, exc,
                    )

    # ------------------------------------------------------------------
    # execution — background (returns task_id immediately)
    # ------------------------------------------------------------------

    async def execute_async(
        self,
        name: str,
        instruction: str,
        context: str = "",
        parent_state: HarnessState | None = None,
        task_id: str | None = None,
    ) -> str:
        """Start a SubAgent task in the background and return a task_id.

        Use ``get_background_result(task_id)`` to poll for completion
        and ``cleanup_background_task(task_id)`` to release memory.

        Returns the task_id for status polling.
        """
        entry = self._agents.get(name)
        if entry is None:
            raise ValueError(f"SubAgent '{name}' 不存在")

        config, tools, llm = entry

        full_instruction = instruction
        if context:
            full_instruction = f"[上下文]\n{context}\n\n[任务]\n{instruction}"

        async with self._semaphore:
            from harness.agents.subagent_executor import SubagentExecutor

            executor = SubagentExecutor(
                config=config,
                llm=llm,
                tools=tools,
                parent_state=parent_state,
            )
            return executor.execute_async(full_instruction, task_id=task_id)

    # ------------------------------------------------------------------
    # background task helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_background_result(task_id: str) -> SubAgentResult | None:
        """Get the result of a background task by ID."""
        return get_background_result(task_id)

    @staticmethod
    def cancel_background_task(task_id: str) -> None:
        """Request cancellation of a running background task."""
        cancel_background_task(task_id)

    @staticmethod
    def cleanup_background_task(task_id: str) -> None:
        """Remove a completed background task from the registry."""
        cleanup_background_task(task_id)
