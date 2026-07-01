#!/usr/bin/env python3
"""Harness main entry point — service bootstrap and wiring."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_openai import ChatOpenAI

from harness.agents.lead_agent import LeadAgent
from harness.agents.lead_agent import _build_middlewares as build_lead_middlewares
from harness.agents.subagent_manager import SubagentManager
from harness.agents.features import RuntimeFeatures
from harness.api.server import HarnessService as _BaseService, set_harness
from harness.config import HarnessConfig, load_config
from harness.config.tool_config import ToolConfig
from harness.config.checkpointer_config import (
    CheckpointerConfig,
    load_checkpointer_config_from_dict,
)
from harness.config.config_manager import ConfigManager
from harness.config.memory_config import MemoryConfig, set_memory_config, get_memory_config
from harness.config.paths import Paths, get_paths, set_paths
from harness.config.yaml_config import DatabaseConfig
from harness.graph_factory import build_harness_graph
from harness.memory.queue import get_memory_queue
from harness.memory.storage import FileMemoryStorage, get_memory_storage
from harness.middleware.base import HarnessAgentMiddleware
from harness.models import (
    AgentNode,
    ClarificationRequest,
    ExecutionGraph,
    HarnessState,
    SubAgentConfig,
    TokenUsage,
    _human_message_with_files,
    initial_state,
)
from harness.observability.langfuse_manager import ObservabilityManager
from harness.persistence.engine import DatabaseEngine
from harness.runtime.checkpointer.async_provider import AsyncCheckpointerProvider
from harness.runtime.events.store import make_event_store
from harness.runtime.events.store.base import RunEventStore
from harness.runtime.journal import RunJournal
from harness.runtime.runs.store import make_run_store
from harness.runtime.runs.store.base import RunStore
from harness.tools.registry import ToolRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HarnessService
# ---------------------------------------------------------------------------


class HarnessService(_BaseService):
    """Full Harness service — wiring, lifecycle, and execution.

    This is set as the concrete implementation via ``set_harness()``
    before the ASGI server starts accepting connections.
    """

    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        features: RuntimeFeatures | None = None,
        config_manager: ConfigManager | None = None,
    ):
        super().__init__()
        self.config = config or load_config()
        self.config_manager = config_manager  # YAML config with mtime hot-reload
        # Bridge YAML config to feature flags
        if features is None:
            features = RuntimeFeatures()
            if self.config_manager:
                title_cfg = self.config_manager.get("title", {})
                if isinstance(title_cfg, dict) and title_cfg.get("enabled", False):
                    features.auto_title = True
        self.features = features
        self.llm: BaseChatModel | None = None
        self.tool_registry = ToolRegistry()
        self.middlewares: list[HarnessAgentMiddleware] = []
        self.observability: ObservabilityManager | None = None
        self.subagent_manager: SubagentManager | None = None
        self.graph: Any = None
        self.sandbox: Any | None = None
        self._active_runs: dict[str, dict[str, Any]] = {}  # 仅保留 cancelled 等运行期标记
        self._active_runs_max: int = 1000                   # 并发容量上限
        self._checkpointer: BaseCheckpointSaver | None = None
        self._db_engine: DatabaseEngine | None = None
        self._run_store: RunStore | None = None
        self._event_store: RunEventStore | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Wire all components and compile the LangGraph graph."""
        if self._initialized:
            return

        async with self._init_lock:
            # 双重检查：获锁后再次确认未被并发初始化
            if self._initialized:
                return

        cfg = self.config

        # 0. Paths singleton + ensure data directory exists
        set_paths(Paths(cfg.data_root))
        paths = get_paths()
        paths.ensure_data_dir()
        logger.info("Data root: %s", paths.base_dir)

        # 1. LLM factories
        self.llm = self._init_llm(cfg.default_model)

        # 2. Load tools declared in config.yaml (DeerFlow-style)
        if self.config_manager is not None:
            raw_tools = self.config_manager.get("tools", [])
            tool_configs = [ToolConfig(**t) for t in raw_tools]
            self.tool_registry.load_tools_from_config(tool_configs)

        # 2.5 Load legacy tool plugins declared in config.yaml
        if self.config_manager is not None:
            plugin_tools = self.config_manager.get("plugins.tools", [])
            self.tool_registry.load_plugins_from_config(plugin_tools)

        # 3. Load MCP tools
        if cfg.mcp_config_path and Path(cfg.mcp_config_path).exists():
            await self.tool_registry.load_mcp_tools(cfg.mcp_config_path)

        # 4. Memory system (DeerFlow-aligned: global singletons)
        memory_cfg_dict: dict[str, Any] = {}
        if self.config_manager is not None:
            memory_cfg_dict = self.config_manager.get("memory") or {}

        # Respect config.yaml memory section when present; fall back to
        # HarnessConfig / RuntimeFeatures defaults otherwise.
        mem_cfg = MemoryConfig(
            enabled=memory_cfg_dict.get("enabled", self.features.memory),
            storage_path=memory_cfg_dict.get("storage_path") or cfg.memory_root,
            debounce_seconds=int(memory_cfg_dict.get("debounce_seconds", cfg.debounce_seconds)),
            model_name=memory_cfg_dict.get("model_name") or cfg.default_model,
            max_facts=int(memory_cfg_dict.get("max_facts", 100)),
            fact_confidence_threshold=float(
                memory_cfg_dict.get("fact_confidence_threshold", 0.7)
            ),
            injection_enabled=memory_cfg_dict.get("injection_enabled", True),
            max_injection_tokens=int(memory_cfg_dict.get("max_injection_tokens", 2000)),
            # ── mem0 配置 ──
            backend=memory_cfg_dict.get("backend", "file"),
            mem0_config=memory_cfg_dict.get("mem0_config", {}),
            mem0_search_top_k=int(memory_cfg_dict.get("mem0_search_top_k", 5)),
            mem0_general_query=memory_cfg_dict.get(
                "mem0_general_query", "用户的偏好、习惯、背景和重要信息",
            ),
            mem0_enable_time_filter=memory_cfg_dict.get("mem0_enable_time_filter", False),
            mem0_recent_days=int(memory_cfg_dict.get("mem0_recent_days", 90)),
        )
        set_memory_config(mem_cfg)
        # Ensure storage singleton is initialized with our root
        FileMemoryStorage(memory_root=cfg.memory_root)
        # Pre-warm the queue singleton
        get_memory_queue()
        # Pre-warm mem0 client if backend is mem0
        if mem_cfg.backend == "mem0":
            from harness.memory.mem0_client import get_mem0
            get_mem0()
        logger.info(
            "Memory system initialized (DeerFlow-aligned): root=%s backend=%s",
            cfg.memory_root, mem_cfg.backend,
        )

        # 5. Observability
        self.observability = ObservabilityManager(cfg)

        # 6. Register middlewares (AgentMiddleware list)
        self._register_middlewares()

        # 8. SubAgent manager (receives middleware list for SubAgent create_agent())
        self.subagent_manager = SubagentManager(
            llm_factory=self._init_llm,
            tool_registry=self.tool_registry,
            middlewares=self.middlewares,
            max_concurrent=cfg.max_concurrent_subagents,
        )

        # 9. Lead Agent (configuration provider — tools + system prompt)
        lead_agent = LeadAgent(
            tool_registry=self.tool_registry,
            subagent_manager=self.subagent_manager,
            max_concurrent_subagents=cfg.max_concurrent_subagents,
        )

        # 10. Checkpointer — load config and create provider
        ckp_cfg = self._load_checkpointer_config()
        ckp_provider = AsyncCheckpointerProvider(ckp_cfg)
        self._checkpointer = await ckp_provider.get_checkpointer()

        # 11. Build graph via create_agent() (memory is middleware-driven)
        self.graph = build_harness_graph(
            llm=self.llm,
            tools=lead_agent.build_tools(),
            middlewares=self.middlewares,
            system_prompt=lead_agent.get_system_prompt(),
            checkpointer=self._checkpointer,
        )

        # 12. Database engine + stores (DeerFlow-aligned persistence)
        db_cfg = self._load_database_config()
        try:
            self._db_engine = DatabaseEngine(db_cfg)
            if self._db_engine.engine is not None:
                await self._db_engine.init_tables()
        except Exception:
            logger.warning("Database engine init failed, persistence disabled")
            self._db_engine = DatabaseEngine(DatabaseConfig(backend="memory"))

        self._run_store = make_run_store()
        self._event_store = make_event_store()
        logger.info("RunStore=%s EventStore=%s", type(self._run_store).__name__, type(self._event_store).__name__)

        self._initialized = True
        logger.info("HarnessService initialized successfully")

    async def shutdown(self) -> None:
        """Clean up resources."""
        try:
            await get_memory_queue().flush()
        except Exception:
            pass
        if self.sandbox:
            await self.sandbox.cleanup()
        # Close checkpointer connections (sqlite / postgres)
        if self._checkpointer is not None:
            closer = getattr(self._checkpointer, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    logger.warning("Error closing checkpointer", exc_info=True)
        # Close database engine
        if self._db_engine is not None:
            try:
                await self._db_engine.close()
            except Exception:
                logger.warning("Error closing database engine", exc_info=True)
        logger.info("HarnessService shut down")

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _init_llm(self, model: str | None = None) -> BaseChatModel:
        model = model or self.config.default_model
        extra_body: dict[str, Any] | None = None
        if self.config.enable_thinking:
            extra_body = {"enable_thinking": True}

        # Qwen3 / DeepSeek 思考模式 — 使用子类保留 reasoning_content
        if extra_body:
            from harness.llm.thinking import ChatOpenAIWithReasoning
            return ChatOpenAIWithReasoning(
                model=model,
                api_key=self.config.openai_api_key or os.getenv("OPENAI_API_KEY", ""),
                base_url=self.config.openai_base_url,
                temperature=0.3,
                extra_body=extra_body,
            )

        return ChatOpenAI(
            model=model,
            api_key=self.config.openai_api_key or os.getenv("OPENAI_API_KEY", ""),
            base_url=self.config.openai_base_url,
            temperature=0.3,
        )

    # ------------------------------------------------------------------
    # checkpointer config
    # ------------------------------------------------------------------

    def _load_checkpointer_config(self) -> CheckpointerConfig:
        """Resolve checkpointer configuration from YAML and/or env vars.

        Priority: YAML ``checkpointer`` section > env vars > defaults.
        """
        data: dict[str, Any] = {}

        # 1. Try YAML ConfigManager first
        if self.config_manager is not None:
            try:
                yaml_ckp: Any = self.config_manager.get("checkpointer")
                if isinstance(yaml_ckp, dict):
                    data.update(yaml_ckp)
            except Exception:
                logger.debug("No checkpointer section in YAML config")

        # 2. Env-var overrides (higher priority)
        env_backend = os.getenv("HARNESS_CHECKPOINTER_BACKEND")
        if env_backend:
            data["backend"] = env_backend
        env_sqlite_dir = os.getenv("HARNESS_CHECKPOINTER_SQLITE_DIR")
        if env_sqlite_dir:
            data["sqlite_dir"] = env_sqlite_dir
        env_pg_url = os.getenv("HARNESS_CHECKPOINTER_POSTGRES_URL")
        if env_pg_url:
            data["postgres_url"] = env_pg_url

        return load_checkpointer_config_from_dict(data)

    def _load_database_config(self) -> DatabaseConfig:
        """Resolve database configuration from YAML and/or env vars.

        Priority: YAML ``database`` section > env vars > defaults.
        """
        data: dict[str, Any] = {}

        if self.config_manager is not None:
            try:
                yaml_db: Any = self.config_manager.get("database")
                if isinstance(yaml_db, dict):
                    data.update(yaml_db)
            except Exception:
                logger.debug("No database section in YAML config")

        env_backend = os.getenv("HARNESS_DATABASE_BACKEND")
        if env_backend:
            data["backend"] = env_backend
        env_sqlite_dir = os.getenv("HARNESS_DATABASE_SQLITE_DIR")
        if env_sqlite_dir:
            data["sqlite_dir"] = env_sqlite_dir
        env_pg_url = os.getenv("HARNESS_DATABASE_POSTGRES_URL")
        if env_pg_url:
            data["postgres_url"] = env_pg_url

        return DatabaseConfig(**data) if data else DatabaseConfig()

    # ------------------------------------------------------------------
    # middlewares
    # ------------------------------------------------------------------

    def _register_middlewares(self) -> None:
        """Build the 20-middleware AgentMiddleware list via lead_agent._build_middlewares.

        Matches DeerFlow's _build_middlewares — 20-middleware design.
        """
        config = RunnableConfig(configurable={
            "workspace_root": self.config.workspace_root,
            "is_plan_mode": self.features.todo is not False,
            "subagent_enabled": self.features.subagent is not False,
            "max_concurrent_subagents": self.config.max_concurrent_subagents,
            "memory_enabled": self.features.memory is not False,
            "summarization_enabled": self.features.summarization is not False,
            "guardrail_enabled": self.features.guardrail is not False,
            "vision_enabled": self.features.vision is not False,
            "tool_search_enabled": False,
            "tool_max_retries": self.config.tool_max_retries,
            "auto_title": self.features.auto_title is not False,
            "title_model": self.config.title_model or self.config.default_model,
            "openai_api_key": self.config.openai_api_key,
            "openai_base_url": self.config.openai_base_url,
        })
        self.middlewares = build_lead_middlewares(
            config,
            config_manager=self.config_manager,
            agent_name=None,
        )
        logger.info("Registered %d AgentMiddlewares (20-middleware DeerFlow-aligned chain)", len(self.middlewares))

    # ------------------------------------------------------------------
    # Langfuse
    # ------------------------------------------------------------------

    def _build_config(self, thread_id: str) -> RunnableConfig:
        """Build RunnableConfig with Langfuse callbacks wired in."""
        cfg = RunnableConfig(
            configurable={"thread_id": thread_id},
            recursion_limit=200,  # 默认 25 不够：11 中间件 + 17 工具时单轮对话 ~50 步
        )
        if self.observability and self.observability.enabled:
            try:
                from langfuse.langchain import CallbackHandler
                # The Langfuse singleton is already configured by
                # ObservabilityManager with the credentials from HarnessConfig.
                # Pass public_key only to select the right client instance.
                handler = CallbackHandler(public_key=self.config.langfuse_public_key)
                cfg["callbacks"] = [handler]
            except Exception as exc:
                logger.warning("Failed to wire Langfuse callback: %s", exc)
        return cfg

    # ------------------------------------------------------------------
    # middleware helpers
    # ------------------------------------------------------------------

    def _get_middleware_by_name(self, name: str) -> Any | None:
        """Return the middleware instance with the given name, or None."""
        for mw in self.middlewares:
            if getattr(mw, "name", None) == name:
                return mw
        return None

    # ------------------------------------------------------------------
    # active runs management
    # ------------------------------------------------------------------

    async def delete_thread(self, thread_id: str, user_id: str = "default") -> dict:
        """Delete all persisted data for a thread.

        Removes the LangGraph checkpoint and the local workspace directory.
        Called by the App service when a user deletes a conversation.
        """
        # 1) Delete LangGraph checkpoint
        if self._checkpointer is not None:
            try:
                await self._checkpointer.adelete_thread(thread_id)
                logger.info("Deleted LangGraph checkpoint for thread %s", thread_id)
            except Exception as exc:
                logger.warning(
                    "Failed to delete checkpoint for thread %s: %s",
                    thread_id, exc,
                )

        # 2) Delete local workspace data (sandbox files, uploads, outputs)
        try:
            paths = get_paths()
            paths.delete_thread_dir(thread_id, user_id=user_id)
            logger.info("Deleted thread workspace for %s (user=%s)", thread_id, user_id)
        except Exception as exc:
            logger.warning(
                "Failed to delete workspace for thread %s: %s",
                thread_id, exc,
            )

        # 3) Clean up middleware state (loop detection counters, etc.)
        self._cleanup_middleware_state(thread_id)

        return {"status": "deleted", "thread_id": thread_id}

    def _cleanup_middleware_state(self, thread_id: str) -> None:
        """通知所有中间件清理 per-thread 状态（修复 #9 字典泄漏）。"""
        for mw in self.middlewares:
            cleaner = getattr(mw, "cleanup_thread", None)
            if cleaner is not None:
                try:
                    cleaner(thread_id)
                except Exception:
                    pass

    def _enforce_capacity(self) -> None:
        """超出并发上限时抛出异常拒绝新请求。"""
        if len(self._active_runs) >= self._active_runs_max:
            raise RuntimeError(
                f"Active runs capacity exceeded ({self._active_runs_max}). "
                f"Please wait for running executions to complete."
            )

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        thread_id: str,
        user_id: str,
        message: str,
        graph: ExecutionGraph | None = None,
        files: list[dict] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute the agent pipeline and stream SSE events in real time.

        Uses ``astream_events`` for true token-level streaming (not
        the old ``ainvoke`` + batch-yield pseudo-streaming).

        Event mapping (Harness → Frontend SSEEventType):
        - ``on_chat_model_stream`` → ``message`` (token-level, appended)
        - ``on_chat_model_end``    → ``token_usage``
        - ``on_tool_start``        → ``tool_call`` / ``subagent_start``
        - ``on_tool_end``          → ``tool_result`` / ``subagent_end``
        - root ``on_chain_end``    → check final state for ``clarification``,
          ``title``, then emit ``finished``
        """
        if not self._initialized:
            await self.initialize()

        build_config = self._build_config(thread_id)

        # ── 从 checkpoint 恢复状态（替旧 _active_runs） ──
        try:
            state_snapshot = await self.graph.aget_state(build_config)
        except Exception:
            logger.warning(
                "aget_state failed for thread=%s, treating as new session",
                thread_id,
            )
            state_snapshot = None

        if state_snapshot is not None and state_snapshot.values:
            # 校验用户归属：同一 thread_id 只能被创建它的 user_id 访问
            existing_user = state_snapshot.values.get("user_id")
            if existing_user and existing_user != user_id:
                logger.warning(
                    "User mismatch for thread=%s — existing=%s request=%s, "
                    "treating as new session",
                    thread_id, existing_user, user_id,
                )
                current_state = initial_state(thread_id, user_id, message, files)
            else:
                # 已有会话，追加新消息（不修改 checkpoint 反序列化出来的对象）
                current_state = dict(state_snapshot.values)
                current_state["messages"] = list(current_state.get("messages", [])) + [
                    _human_message_with_files(message, files)
                ]
        else:
            # 新会话
            current_state = initial_state(thread_id, user_id, message, files)

        # ── 注册运行期取消标记（不保存完整 state） ──
        self._active_runs[thread_id] = {"cancelled": False}
        try:
            self._enforce_capacity()
        except RuntimeError as e:
            self._active_runs.pop(thread_id, None)
            yield {"type": "error", "content": str(e), "thread_id": thread_id}
            return

        # Start Langfuse trace
        trace_id = ""
        if self.observability:
            trace_id = self.observability.start_trace(thread_id, user_id)

        # ── 持久化：Run/Thread/Event 元数据 ──
        run_id = str(uuid.uuid4())
        logger.info("Starting run_id=%s thread=%s user=%s", run_id, thread_id, user_id)

        # Ensure sandbox directories exist
        try:
            get_paths().ensure_thread_dirs(thread_id, user_id=user_id)
        except Exception:
            logger.warning("Failed to create thread dirs for %s/%s", thread_id, user_id)

        # Make run_id available to middlewares via configurable
        build_config["configurable"]["run_id"] = run_id

        # Bind journal to LangChain callbacks
        journal = RunJournal(thread_id, run_id, user_id, self._event_store) if self._event_store else None
        if journal:
            journal.set_first_human_message(message)
            existing_callbacks: list = build_config.get("callbacks") or []
            build_config["callbacks"] = [*existing_callbacks, journal]

        # Select which graph to run
        if graph and graph.nodes:
            runner = self._build_custom_graph(graph)
        elif self.graph is not None:
            runner = self.graph
        else:
            yield {"type": "error", "content": "Harness graph is not initialized"}
            return

        # Emit start event (run_id available for frontend to query events later)
        yield {
            "type": "message", "content": "", "thread_id": thread_id,
            "status": "started", "trace_id": trace_id, "run_id": run_id,
        }

        # Per-run streaming state
        final_state: dict[str, Any] = {}
        active_subagents: dict[str, str] = {}  # run_id → subagent_name
        _collected_token_usage: dict[str, Any] = {}
        _title_emitted = False  # 防止重复发送 title_update

        try:
            async for event in runner.astream_events(current_state, build_config, version="v2"):
                # ── 检查取消标志（支持 stop() 中断执行） ──
                if self._active_runs.get(thread_id, {}).get("cancelled"):
                    raise asyncio.CancelledError("Execution cancelled by user")
                kind = event["event"]
                evt_name = event.get("name", "")
                evt_data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]
                evt_tags: list[str] = event.get("tags", []) or []  # type: ignore[assignment]

                # ── Token-level streaming ──────────────────────────
                if kind == "on_chat_model_stream":
                    chunk: Any = evt_data.get("chunk")
                    if chunk is None:
                        continue

                    # ── 思考过程（Qwen3 / DeepSeek reasoning_content）──
                    reasoning = (
                        getattr(chunk, "reasoning_content", None)
                        or getattr(chunk, "additional_kwargs", {}).get("reasoning_content", "")
                    )
                    if reasoning and isinstance(reasoning, str):
                        yield {
                            "type": "thinking",
                            "content": reasoning,
                            "thread_id": thread_id,
                        }

                    content = getattr(chunk, "content", "")
                    if not content:
                        continue
                    # content may be str or list[dict]
                    if isinstance(content, list):
                        content = "".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    if content:
                        yield {
                            "type": "message",
                            "content": str(content),
                            "thread_id": thread_id,
                        }

                # ── Token usage (per LLM call) ─────────────────────
                elif kind == "on_chat_model_end":
                    output: Any = evt_data.get("output")
                    if output is None:
                        continue
                    usage_meta = (
                        getattr(output, "usage_metadata", None)
                        or getattr(output, "response_metadata", {}).get("token_usage", {})
                    )
                    if usage_meta:
                        usage = TokenUsage(
                            prompt_tokens=usage_meta.get("input_tokens", 0),
                            completion_tokens=usage_meta.get("output_tokens", 0),
                            total_tokens=usage_meta.get("total_tokens", 0),
                            cost_usd=0,
                        )
                        _collected_token_usage = usage
                        yield {
                            "type": "token_usage",
                            "thread_id": thread_id,
                            "tokens": usage.model_dump(),
                        }

                # ── Tool start ─────────────────────────────────────
                elif kind == "on_tool_start":
                    tool_name = evt_name  # tool name == event name
                    tool_input: Any = evt_data.get("input", {})
                    run_id: str = event.get("run_id", "")

                    # Detect SubAgent dispatch (the ``task`` tool)
                    if tool_name == "task" and isinstance(tool_input, dict):
                        sub_name = tool_input.get("subagent_type", tool_input.get("name", "unknown"))
                        active_subagents[run_id] = str(sub_name)
                        yield {
                            "type": "subagent_start",
                            "thread_id": thread_id,
                            "subagent_name": str(sub_name),
                            "instruction": str(tool_input.get("description", "")),
                        }
                    elif tool_name not in ("task",):
                        yield {
                            "type": "tool_call",
                            "thread_id": thread_id,
                            "tool_name": tool_name,
                            "tool_args": tool_input if isinstance(tool_input, dict) else {"input": str(tool_input)},
                        }

                # ── Tool end ───────────────────────────────────────
                elif kind == "on_tool_end":
                    tool_name = evt_name
                    run_id: str = event.get("run_id", "")
                    tool_output: Any = evt_data.get("output", "")

                    if tool_name == "task":
                        # SubAgent completion
                        sub_name = active_subagents.pop(run_id, "unknown")
                        # Prefer ToolMessage content when available
                        output_str = ""
                        if hasattr(tool_output, "content"):
                            output_str = str(tool_output.content)
                        elif isinstance(tool_output, str):
                            output_str = tool_output
                        else:
                            output_str = str(tool_output)
                        yield {
                            "type": "subagent_end",
                            "thread_id": thread_id,
                            "subagent_name": sub_name,
                            "content": output_str[:2000],  # cap for UI
                            "status": "done",
                        }
                    elif tool_name not in ("task",):
                        output_str = ""
                        if hasattr(tool_output, "content"):
                            output_str = str(tool_output.content)
                        elif isinstance(tool_output, str):
                            output_str = tool_output
                        else:
                            output_str = str(tool_output)
                        yield {
                            "type": "tool_result",
                            "thread_id": thread_id,
                            "tool_name": tool_name,
                            "tool_result": output_str[:2000],  # cap for UI
                        }

                # ── Root graph completion ──────────────────────────
                elif kind == "on_chain_end" and evt_name == "LangGraph":
                    result: Any = evt_data.get("output")
                    if isinstance(result, dict):
                        final_state = result

            # ── Post-stream: middleware-injected state fields ──────
            # After astream_events completes, final_state contains
            # the full HarnessState with middleware-modified fields.
            # State is automatically checkpointed by LangGraph — no manual
            # save to _active_runs needed.

            # Log aggregated token usage
            if _collected_token_usage and self.observability:
                self.observability.log_token_usage(trace_id, "", _collected_token_usage)

            # 标题已作为 HarnessState 字段被 checkpoint 持久化
            suggested_title = final_state.get("suggested_title")
            if suggested_title and not _title_emitted:
                _title_emitted = True
                yield {
                    "type": "title_update",
                    "thread_id": thread_id,
                    "title": suggested_title,
                }

            # Emit pending clarification if any
            pending: Any = final_state.get("pending_clarification")
            if pending:
                # Write run as suspended (awaiting clarification)
                if self._run_store:
                    completion = journal.get_completion_data() if journal else {}
                    await self._run_store.update_run_completion(
                        run_id, "pending", **completion,
                    )
                yield {
                    "type": "clarification",
                    "request": pending if isinstance(pending, dict) else pending.model_dump(),
                    "thread_id": thread_id,
                }
                if self.observability:
                    self.observability.finalize_trace(trace_id, "suspended")
                return

            # ── Run success ──
            if self._run_store:
                completion = journal.get_completion_data() if journal else {}
                await self._run_store.update_run_completion(
                    run_id, "success", **completion,
                )

            # Emit finished
            yield {"type": "finished", "thread_id": thread_id, "run_id": run_id}
            if self.observability:
                self.observability.finalize_trace(trace_id, "success")

        except asyncio.CancelledError:
            if self._run_store:
                await self._run_store.update_status(run_id, "interrupted")
            yield {"type": "error", "content": "执行已取消", "thread_id": thread_id}
            if self.observability:
                self.observability.finalize_trace(trace_id, "cancelled")
        except Exception as exc:
            logger.exception("Execution failed for thread=%s", thread_id)
            if self._run_store:
                await self._run_store.update_status(run_id, "error", error=str(exc))
            yield {"type": "error", "content": str(exc), "thread_id": thread_id}
            if self.observability:
                self.observability.finalize_trace(trace_id, "error")
        finally:
            if journal:
                await journal.flush()
            # ── 清理中间件线程状态（修复 #9 字典泄漏） ──
            pending_in_final = final_state.get("pending_clarification") if final_state else None
            if not pending_in_final:
                self._cleanup_middleware_state(thread_id)
            # 正常完成（无 pending_clarification）时清理运行期标记
            # 有 pending_clarification 时保留标记，以便 stop() 可取消
            if not pending_in_final:
                self._active_runs.pop(thread_id, None)

    async def respond_to_clarification(
        self,
        thread_id: str,
        clarification_id: str,
        answer: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Resume execution after a clarification answer, streaming results.

        Reads the suspended state from LangGraph checkpoint (via ``aget_state``)
        instead of the old in-memory ``_active_runs`` dict.
        """
        build_config = self._build_config(thread_id)

        # ── 从 checkpoint 读取暂停状态 ──
        try:
            state_snapshot = await self.graph.aget_state(build_config)
        except Exception:
            logger.exception("aget_state failed for thread=%s", thread_id)
            yield {"type": "error", "content": f"thread '{thread_id}' not found", "thread_id": thread_id}
            return

        if state_snapshot is None or not state_snapshot.values:
            yield {"type": "error", "content": f"thread '{thread_id}' not active", "thread_id": thread_id}
            return

        state = dict(state_snapshot.values)
        pending = state.get("pending_clarification")
        if pending is None:
            yield {"type": "error", "content": "no pending clarification", "thread_id": thread_id}
            return

        # Update pending clarification with the answer — create a new object
        # instead of mutating the checkpoint-deserialized value.
        if isinstance(pending, ClarificationRequest):
            pending = pending.model_copy(
                update={"answer": answer, "resolved_at": datetime.now()}
            )
        else:
            pending = {
                **pending,
                "answer": answer,
                "resolved_at": datetime.now().isoformat(),
            }
        state["pending_clarification"] = pending

        # ── 注册运行期取消标记 ──
        self._active_runs[thread_id] = {"cancelled": False}

        # Emit start event
        yield {"type": "message", "content": "", "thread_id": thread_id, "status": "resumed"}

        final_state: dict[str, Any] = {}
        try:
            async for event in self.graph.astream_events(state, build_config, version="v2"):
                kind = event["event"]
                evt_name = event.get("name", "")
                evt_data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]

                if kind == "on_chat_model_stream":
                    chunk: Any = evt_data.get("chunk")
                    if chunk is None:
                        continue

                    # ── 思考过程（Qwen3 / DeepSeek reasoning_content）──
                    reasoning = (
                        getattr(chunk, "reasoning_content", None)
                        or getattr(chunk, "additional_kwargs", {}).get("reasoning_content", "")
                    )
                    if reasoning and isinstance(reasoning, str):
                        yield {
                            "type": "thinking",
                            "content": reasoning,
                            "thread_id": thread_id,
                        }

                    content = getattr(chunk, "content", "")
                    if not content:
                        continue
                    if isinstance(content, list):
                        content = "".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    if content:
                        yield {"type": "message", "content": str(content), "thread_id": thread_id}

                elif kind == "on_tool_start":
                    tool_name = evt_name
                    tool_input: Any = evt_data.get("input", {})
                    if tool_name == "task":
                        sub_name = tool_input.get("subagent_type", tool_input.get("name", "unknown")) if isinstance(tool_input, dict) else "unknown"
                        yield {
                            "type": "subagent_start", "thread_id": thread_id,
                            "subagent_name": str(sub_name),
                            "instruction": str(tool_input.get("description", "")) if isinstance(tool_input, dict) else "",
                        }
                    elif tool_name not in ("task",):
                        yield {
                            "type": "tool_call", "thread_id": thread_id,
                            "tool_name": tool_name,
                            "tool_args": tool_input if isinstance(tool_input, dict) else {"input": str(tool_input)},
                        }

                elif kind == "on_tool_end":
                    tool_name = evt_name
                    tool_output: Any = evt_data.get("output", "")
                    output_str = ""
                    if hasattr(tool_output, "content"):
                        output_str = str(tool_output.content)
                    elif isinstance(tool_output, str):
                        output_str = tool_output
                    else:
                        output_str = str(tool_output)
                    yield {
                        "type": "tool_result", "thread_id": thread_id,
                        "tool_name": tool_name,
                        "tool_result": output_str[:2000],
                    }

                elif kind == "on_chain_end" and evt_name == "LangGraph":
                    result: Any = evt_data.get("output")
                    if isinstance(result, dict):
                        final_state = result

            # Post-stream: check for new clarifications
            new_pending = final_state.get("pending_clarification")
            if new_pending and not (
                hasattr(new_pending, "answer") and new_pending.answer
            ):
                # State is auto-checkpointed by LangGraph — no manual save needed
                yield {
                    "type": "clarification",
                    "request": new_pending if isinstance(new_pending, dict) else new_pending.model_dump(),
                    "thread_id": thread_id,
                }
                return

            # Success — clean up runtime flag only (checkpoint persists)
            self._active_runs.pop(thread_id, None)
            yield {"type": "finished", "thread_id": thread_id}

        except Exception as exc:
            logger.exception("Resumption failed for thread=%s", thread_id)
            self._active_runs.pop(thread_id, None)
            yield {"type": "error", "content": str(exc), "thread_id": thread_id}

    async def stop(self, thread_id: str) -> None:
        """Cancel a running execution.

        Only sets the cancelled flag on the runtime marker — checkpoint
        state is preserved so the user can resume later if needed.
        """
        run = self._active_runs.get(thread_id)
        if run is not None:
            run["cancelled"] = True

    async def get_status(self, thread_id: str) -> dict[str, Any]:
        """Return thread execution status, reading from LangGraph checkpoint."""
        build_config = self._build_config(thread_id)

        try:
            state_snapshot = await self.graph.aget_state(build_config)
        except Exception:
            logger.warning("aget_state failed for thread=%s", thread_id)
            return {"thread_id": thread_id, "status": "inactive"}

        if state_snapshot is None or not state_snapshot.values:
            return {"thread_id": thread_id, "status": "inactive"}

        state = state_snapshot.values
        pending = state.get("pending_clarification")

        # Check runtime cancelled marker
        active_run = self._active_runs.get(thread_id)
        if active_run and active_run.get("cancelled"):
            return {"thread_id": thread_id, "status": "cancelling"}

        return {
            "thread_id": thread_id,
            "status": "suspended" if pending else "running",
        }

    # ------------------------------------------------------------------
    # custom graph builder (frontend canvas support)
    # ------------------------------------------------------------------

    def _build_custom_graph(self, execution_graph: ExecutionGraph) -> Any:
        """Build a LangGraph StateGraph from user-defined nodes and edges.

        This enables the frontend canvas to define custom agent pipelines.
        Each AgentNode in the execution graph is compiled into a SubAgent
        execution node; edges define the data-flow topology.
        """
        # Validate SubAgent configs
        invalid = [
            n.id for n in execution_graph.nodes
            if n.type == "subagent" and (not n.config.name or not n.config.name.strip())
        ]
        if invalid:
            raise ValueError(
                f"以下 SubAgent 节点缺少 name: {invalid}。请在画布中为每个 SubAgent 填写 name 字段。"
            )

        from langgraph.graph import END, StateGraph

        custom = StateGraph(HarnessState)

        # Add a node for each agent in the canvas
        for node in execution_graph.nodes:
            node_id = node.id
            if node.type == "lead":
                # Lead agent node — uses the full agent subgraph (create_agent)
                async def _lead_node(
                    state: HarnessState,
                    _node_id: str = node_id,  # 默认参数捕获，防止闭包共享
                ) -> HarnessState:
                    if self.graph is None:
                        return state
                    from langgraph.config import get_config
                    cfg = get_config()
                    return await self.graph.nodes["agent"].ainvoke(state, cfg)
                custom.add_node(node_id, _lead_node)
            else:
                # SubAgent node — creates a SubAgent inline and executes
                sub_config = node.config

                async def _make_subagent_node(
                    state: HarnessState,
                    _cfg: SubAgentConfig = sub_config,
                ) -> dict[str, Any] | None:
                    if self.subagent_manager is None:
                        return None
                    # Create SubAgent if not already registered
                    if self.subagent_manager.get(_cfg.name) is None:
                        await self.subagent_manager.create(_cfg)
                    # Execute with the last user message as instruction
                    last_user = None
                    for m in reversed(state.get("messages", [])):
                        if hasattr(m, "content") and not getattr(m, "tool_calls", None):
                            last_user = str(m.content)
                            break
                    if last_user:
                        result = await self.subagent_manager.execute(
                            _cfg.name,
                            last_user,
                            parent_state=state,
                        )
                        return {"subagent_results": {_cfg.name: result.model_dump()}}
                    return None

                custom.add_node(node_id, _make_subagent_node)

        # Set entry point
        custom.set_entry_point(execution_graph.entry_point)

        # Add edges
        edge_targets: dict[str, list[str]] = {}
        for node in execution_graph.nodes:
            edge_targets[node.id] = list(node.connections)

        # Track nodes that already have outgoing edges set by a chain
        _has_outgoing: set[str] = set()

        for source, targets in edge_targets.items():
            # Skip nodes whose outgoing edges were set by a sequential chain
            if source in _has_outgoing:
                continue

            if not targets:
                custom.add_edge(source, END)
                _has_outgoing.add(source)
            elif len(targets) == 1:
                custom.add_edge(source, targets[0])
                _has_outgoing.add(source)
            else:
                # ── Sequential chain for multi-target nodes ──
                # source → targets[0] → targets[1] → ... → targets[-1]
                custom.add_edge(source, targets[0])
                _has_outgoing.add(source)

                for i in range(len(targets) - 1):
                    curr, nxt = targets[i], targets[i + 1]
                    if curr not in _has_outgoing:
                        custom.add_edge(curr, nxt)
                        _has_outgoing.add(curr)

                # Last target: if it has no outgoing connections → END
                last = targets[-1]
                if not edge_targets.get(last):
                    custom.add_edge(last, END)
                    _has_outgoing.add(last)

        # Any remaining leaf nodes without outgoing edges → END
        for node in execution_graph.nodes:
            if node.id not in _has_outgoing:
                targets = edge_targets.get(node.id, [])
                if not targets:
                    custom.add_edge(node.id, END)

        logger.info(
            "Built custom execution graph — %d nodes, entry=%s",
            len(execution_graph.nodes),
            execution_graph.entry_point,
        )
        return custom.compile(checkpointer=self._checkpointer)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    import uvicorn

    from harness.api.server import app

    config = load_config()

    # ── 自动加载 YAML 配置（如果存在 config.yaml） ──
    yaml_path = Path(__file__).resolve().parent / "config.yaml"
    config_mgr: ConfigManager | None = None
    if yaml_path.exists():
        config_mgr = ConfigManager(config_path=str(yaml_path))
        config_mgr.load()
        logger.info("Loaded config.yaml from %s", yaml_path)

    harness = HarnessService(config, config_manager=config_mgr)
    await harness.initialize()
    set_harness(harness)

    config_obj = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=config.harness_port,
        log_level="info",
    )
    server = uvicorn.Server(config_obj)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
