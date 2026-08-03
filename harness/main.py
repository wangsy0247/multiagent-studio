#!/usr/bin/env python3
"""Harness main entry point — service bootstrap and wiring."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_openai import ChatOpenAI


# ---------------------------------------------------------------------------
# Per-(user, agent) cached compilation
# ---------------------------------------------------------------------------

@dataclass
class GraphContext:
    """Per-(user_id, agent_name) 缓存的完整编译结果."""
    effective_config: EffectiveConfig
    llm: BaseChatModel
    middlewares: list  # list[HarnessAgentMiddleware]
    graph: Any         # CompiledStateGraph
    lead_agent: Any    # LeadAgent


# Per-task user credentials — 供 _init_llm 在子 agent 创建时读取
# 避免并发用户相互覆盖共享的 SubagentManager._llm_factory
_current_req_creds: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "_current_req_creds", default={},
)


def parse_slash_command(message: str) -> str | None:
    """识别聊天输入中的斜杠指令 (目前: /compact, /clear), 容忍大小写与首尾空白."""
    cmd = (message or "").strip().lower()
    return cmd if cmd in ("/compact", "/clear") else None


def _get_memory_context(user_id: str) -> str:
    """Extract memory facts for *user_id* to pass to the review fork.

    Returns a ``<memory>`` XML block, or an empty string.
    """
    try:
        from harness.memory.updater import get_memory_data

        data = get_memory_data(agent_name=None, user_id=user_id)
        if not data:
            return ""
        facts = data.get("facts", [])
        if not facts:
            return ""
        # Build compact memory block (top 20 facts by confidence)
        facts_sorted = sorted(
            facts, key=lambda f: f.get("confidence", 0), reverse=True,
        )[:20]
        lines = ["<memory>"]
        for f in facts_sorted:
            content = str(f.get("content", ""))[:200]
            category = f.get("category", "")
            lines.append(f"  - [{category}] {content}")
        lines.append("</memory>")
        return "\n".join(lines)
    except Exception:
        return ""

from harness.agents.lead_agent import LeadAgent
from harness.agents.lead_agent import _build_middlewares as build_lead_middlewares
from harness.agents.subagent_manager import SubagentManager
from harness.agents.features import RuntimeFeatures
from harness.api.server import HarnessService as _BaseService, set_harness
from harness.config import HarnessConfig, ConfigLoader, EffectiveConfig, load_config
from harness.config.tool_config import ToolConfig
from harness.config.checkpointer_config import (
    CheckpointerConfig,
    load_checkpointer_config_from_dict,
)
from harness.config.config_manager import ConfigManager
from harness.config.memory_config import MemoryConfig, set_memory_config
from harness.config.paths import Paths, get_paths, set_paths
from harness.config.yaml_config import DatabaseConfig
from harness.graph_factory import build_harness_graph
from harness.memory.queue import get_memory_queue
from harness.memory.storage import FileMemoryStorage
from harness.middleware.base import HarnessAgentMiddleware
from harness.middleware.clarification import get_pending_clarification
from harness.models import (
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

# Team 模式澄清暂停 TTL — 超过此时长未回答的暂停 run 懒过期为 cancelled
CLARIFICATION_TTL_S = 1800


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
        # RuntimeFeatures — 保留向后兼容, 但 EffectiveConfig 优先
        if features is None:
            features = RuntimeFeatures()
        self.features = features

        self.tool_registry = ToolRegistry()
        self.observability: ObservabilityManager | None = None
        self.subagent_manager: SubagentManager | None = None
        self.sandbox: Any | None = None
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._active_runs_max: int = 1000
        self._checkpointer: BaseCheckpointSaver | None = None
        self._db_engine: DatabaseEngine | None = None
        self._run_store: RunStore | None = None
        self._event_store: RunEventStore | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

        # Per-(user_id, agent_name) graph cache — lazily populated by execute()
        self._graph_cache: dict[tuple[str, str], GraphContext] = {}
        self._graph_cache_lock = asyncio.Lock()

        # ── Skill self-evolution — per-thread counter + review fork ──
        # 计数器跨轮累积 (同一 thread_id), 达到阈值触发后台 review 后归零.
        self._thread_skill_iters: dict[str, int] = {}
        self._skill_nudge_interval: int = 10  # trigger after 10 tool iterations

        # ── Curator idle tracking ──
        self._last_user_activity: dict[str, object] = {}  # user_id → datetime

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, *, agent_name: str = "default", user_id: str = "default") -> None:
        """初始化共享基础设施 (paths, tools, memory, observability, checkpointer, DB).

        不再创建 LLM / middlewares / graph — 这些延迟到首次 execute() 时按用户编译。

        Args:
            agent_name: 用于 bootstrap (加载初始 EffectiveConfig 以读取基础设施字段)
            user_id: 用户 ID
        """
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

        cfg = self.config

        # 0. 预检: 检查必需配置并提供引导
        self._bootstrap_check(user_id)

        # 0.1. Paths singleton
        set_paths(Paths(cfg.data_root))
        paths = get_paths()
        paths.ensure_data_dir()
        logger.info("Data root: %s", paths.base_dir)

        # 0.5. Ensure default agent exists
        self._ensure_default_agent(user_id)

        # 1. 加载 bootstrap EffectiveConfig (用于读取基础设施字段)
        bootstrap_eff = ConfigLoader.load_effective(
            user_id=user_id, agent_name=agent_name,
        )
        logger.info(
            "Bootstrap EffectiveConfig: agent=%s model=%s tool_groups=%s",
            agent_name, bootstrap_eff.model, bootstrap_eff.tool_groups,
        )

        # 2. Load tools from harness config.yaml (fallback tool definitions)
        if self.config_manager is not None:
            raw_tools = self.config_manager.get("tools", [])
            tool_configs = [ToolConfig(**t) for t in raw_tools]
            self.tool_registry.load_tools_from_config(tool_configs)
            plugin_tools = self.config_manager.get("plugins.tools", [])
            self.tool_registry.load_plugins_from_config(plugin_tools)

        # 2.5 Summarization config — 把 config.yaml 的 trigger/keep 注入中间件单例,
        # 否则 create_summarization_middleware 读到 trigger=None 永远不会触发压缩.
        if self.config_manager is not None:
            from harness.config.summarization_config import (
                load_summarization_config_from_dict,
                set_summarization_config,
            )
            _summ_raw = self.config_manager.get("summarization")
            set_summarization_config(load_summarization_config_from_dict(_summ_raw))

        # 3. MCP tools
        mcp_path = cfg.mcp_config_path or "./extensions_config.json"
        await self.tool_registry.load_mcp_tools(mcp_path)

        # 4. Memory system — 基础设施默认值 (per-user 字段由 middleware config 覆盖)
        memory_cfg_dict = bootstrap_eff.raw.get("memory", {})
        mem_cfg = MemoryConfig(
            # 基础设施 (全局部署级)
            storage_path=memory_cfg_dict.get("storage_path") or cfg.memory_root,
            debounce_seconds=int(bootstrap_eff.memory_debounce_seconds),
            # 默认值 (per-user 字段通过 middleware config → queue → updater 覆盖)
            enabled=True,
            model_name="",
            api_key="",
            base_url="",
            max_facts=bootstrap_eff.memory_max_facts,
            memory_ttl_days=bootstrap_eff.memory_ttl_days,
            fact_confidence_threshold=bootstrap_eff.memory_fact_confidence_threshold,
            injection_enabled=bootstrap_eff.memory_injection_enabled,
            max_injection_tokens=bootstrap_eff.memory_max_injection_tokens,
        )
        set_memory_config(mem_cfg)
        FileMemoryStorage(memory_root=cfg.memory_root)
        get_memory_queue()
        logger.info(
            "Memory system initialized: project_memory=%s",
            "enabled" if mem_cfg.project_memory_enabled else "disabled",
        )

        # 5. Observability — bootstrap_eff 驱动 (基础设施)
        langfuse_cfg = {
            "langfuse_enabled": bootstrap_eff.langfuse_enabled,
            "langfuse_public_key": bootstrap_eff.langfuse_public_key,
            "langfuse_secret_key": bootstrap_eff.langfuse_secret_key,
            "langfuse_host": bootstrap_eff.langfuse_host,
        }
        self.observability = ObservabilityManager(langfuse_cfg)

        # 6. Skill storage (基础设施, 所有用户共享)
        from harness.skills.storage import SkillStorage
        _project_skills_root = (
            Path(os.path.dirname(os.path.abspath(__file__))).parent / "skills"
        )
        _project_skills_root.mkdir(parents=True, exist_ok=True)
        (_project_skills_root / "builtin").mkdir(exist_ok=True)
        # 6b. Skills: builtin 复制到 data_root (Docker 可访问), my/ 用 symlink.
        # LocalSandbox 使用专用 PathMapping, 不使用这里的复制目录.
        _data_skills_root = Path(self.config.data_root).expanduser().resolve() / "skills"
        _data_skills_root.mkdir(parents=True, exist_ok=True)
        from harness.config.paths import set_skills_root, sync_builtin_skills
        set_skills_root(_data_skills_root)
        sync_builtin_skills(_data_skills_root)
        logger.info("Skills root: data=%s project=%s", _data_skills_root, _project_skills_root)

        _user_skills_base = Path(self.config.data_root).expanduser().resolve() / "users"
        self.skill_storage = SkillStorage(
            _project_skills_root,
            user_skills_base=_user_skills_base,
        )
        self.skills = self.skill_storage.load_skills(enabled_only=True)
        logger.info("Skills loaded: %d enabled", len(self.skills))

        # 7. Worktree config
        from harness.worktree.types import WorktreeConfig as WTCfg
        _wt_cfg = WTCfg(enabled=bootstrap_eff.worktree_enabled)
        if self.config_manager is not None:
            _wt_raw = self.config_manager.get("worktree") or {}
            if isinstance(_wt_raw, dict):
                _wt_cfg = WTCfg(
                    enabled=bootstrap_eff.worktree_enabled,
                    auto_init=_wt_raw.get("auto_init", True),
                    symlink_deps=_wt_raw.get("symlink_deps", [".venv", "node_modules"]),
                    keep_on_conflict=_wt_raw.get("keep_on_conflict", True),
                    cleanup_stale_on_start=_wt_raw.get("cleanup_stale_on_start", True),
                )

        self.subagent_manager = SubagentManager(
            llm_factory=self._init_llm,
            tool_registry=self.tool_registry,
            max_concurrent=bootstrap_eff.max_concurrent_subagents,
            skill_storage=self.skill_storage,
            worktree_config=_wt_cfg,
        )
        if _wt_cfg.enabled and _wt_cfg.cleanup_stale_on_start:
            await self._cleanup_stale_worktrees()

        # 8. Checkpointer
        ckp_cfg = self._load_checkpointer_config()
        ckp_cfg.backend = bootstrap_eff.checkpointer_backend
        ckp_provider = AsyncCheckpointerProvider(ckp_cfg)
        self._checkpointer = await ckp_provider.get_checkpointer()

        # 9. Database engine
        db_cfg = DatabaseConfig(backend=bootstrap_eff.database_backend, sqlite_dir=bootstrap_eff.database_sqlite_dir)
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

        # ── 后台 curator / lifecycle 调度 (不阻塞启动) ──
        try:
            from harness.skills.evolution.curator import maybe_run_curator

            _model = eff.model
            _skill_storage = self.skill_storage
            _llm_factory = self._init_llm

            async def _schedule_curator():
                # 短暂延迟, 等基础设施完全就绪
                await asyncio.sleep(5)
                try:
                    await maybe_run_curator(
                        user_id, _skill_storage, _llm_factory, _model,
                    )
                except Exception:
                    pass

            asyncio.create_task(_schedule_curator())
        except Exception:
            logger.debug("Curator scheduling skipped", exc_info=True)

        self._initialized = True
        logger.info("HarnessService infrastructure initialized (user=%s agent=%s)", user_id, agent_name)

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
        self._graph_cache.clear()
        logger.info("HarnessService shut down")

    def invalidate_graph_cache(self, user_id: str | None = None) -> int:
        """Invalidate cached graph contexts so they are rebuilt on next request.

        Called by the skills API layer after any mutation that affects the
        system prompt (toggle, create, update, delete, install, rollback).

        Args:
            user_id: If provided, only invalidate entries for this user.
                     If ``None``, invalidate **all** cached entries.

        Returns:
            Number of entries removed.
        """
        if user_id is not None:
            keys = [k for k in self._graph_cache if k[0] == user_id]
        else:
            keys = list(self._graph_cache)
        for k in keys:
            del self._graph_cache[k]
        if keys:
            logger.info(
                "Graph cache invalidated: %d entries (user=%s)",
                len(keys), user_id or "*",
            )
        return len(keys)

    # ------------------------------------------------------------------
    # Per-(user, agent) graph context — lazy compilation
    # ------------------------------------------------------------------

    async def _get_or_create_graph_context(
        self, user_id: str, agent_name: str,
    ) -> GraphContext:
        """返回 (user_id, agent_name) 的缓存 GraphContext, 未命中则编译.

        双重检查锁模式 — 避免并发请求重复编译.
        """
        if not self._initialized:
            raise RuntimeError("HarnessService not initialized — call initialize() first")

        key = (user_id, agent_name)
        # 快速路径: 无锁读取
        if key in self._graph_cache:
            return self._graph_cache[key]

        async with self._graph_cache_lock:
            # 获取锁后再次检查 (可能已被并发请求编译)
            if key in self._graph_cache:
                return self._graph_cache[key]
            ctx = await self._build_graph_context(user_id, agent_name)
            self._graph_cache[key] = ctx
            logger.info(
                "Cached graph context: user=%s agent=%s model=%s middlewares=%d",
                user_id, agent_name, ctx.effective_config.model, len(ctx.middlewares),
            )
            return ctx

    async def _build_graph_context(
        self, user_id: str, agent_name: str,
    ) -> GraphContext:
        """加载配置 → 创建 LLM → 创建中间件 → 编译 graph → 返回 GraphContext."""
        # 1. 加载该用户的 EffectiveConfig (L0+L1+L2 merge)
        eff = ConfigLoader.load_effective(user_id=user_id, agent_name=agent_name)
        logger.info(
            "Building graph context: user=%s agent=%s model=%s tool_groups=%s",
            user_id, agent_name, eff.model, eff.tool_groups,
        )

        # 2. 创建 per-user LLM
        llm = self._init_llm(
            eff.model,
            api_key=eff.api_key,
            base_url=eff.base_url,
            temperature=eff.temperature,
            max_tokens=eff.max_tokens,
        )

        # 3. 创建 per-user middlewares
        middlewares = self._build_middlewares_for(eff, user_id, agent_name=agent_name)

        # 4. 设置 contextvar — 子 agent 通过 _init_llm 读取当前用户凭证和模型
        _current_req_creds.set({
            "api_key": eff.api_key,
            "base_url": eff.base_url,
            "model": eff.model,
        })

        # 5. 设置 skill 操作的当前用户 ID (skill_manage 仅后台 review fork 可用)
        from harness.tools.skill_manage_tool import set_skill_user_id
        set_skill_user_id(user_id)

        # 6. 创建 per-user LeadAgent
        lead_agent = LeadAgent(
            tool_registry=self.tool_registry,
            subagent_manager=self.subagent_manager,
            max_concurrent_subagents=eff.max_concurrent_subagents,
            config_manager=self.config_manager,
            skill_storage=self.skill_storage,
            agent_name=eff.agent_display_name or agent_name,
            user_id=user_id,
            agent_soul=eff.agent_soul,
        )

        # 7. 编译 graph
        graph = build_harness_graph(
            llm=llm,
            tools=lead_agent.build_tools(),
            middlewares=middlewares,
            system_prompt=lead_agent.get_system_prompt(),
            checkpointer=self._checkpointer,
        )

        return GraphContext(
            effective_config=eff,
            llm=llm,
            middlewares=middlewares,
            graph=graph,
            lead_agent=lead_agent,
        )

    def _build_middlewares_for(
        self, eff: EffectiveConfig, user_id: str, *, agent_name: str | None = None,
    ) -> list:
        """根据 EffectiveConfig 构建中间件列表 (per-user + per-agent)."""
        config = RunnableConfig(configurable={
            "workspace_root": self.config.workspace_root,
            "is_plan_mode": eff.subagent_enabled,
            "subagent_enabled": eff.subagent_enabled,
            "max_concurrent_subagents": eff.max_concurrent_subagents,
            "memory_enabled": eff.memory_injection_enabled,
            "summarization_enabled": eff.summarization_enabled,
            "guardrail_enabled": eff.guardrail_enabled,
            "vision_enabled": getattr(self.features, 'vision', False),
            "tool_search_enabled": False,
            "tool_max_retries": self.config.tool_max_retries,
            "auto_title": eff.title_enabled,
            "title_model": eff.title_model or eff.model or "gpt-4o-mini",
            "summary_model": eff.summary_model or eff.model or "gpt-4o",
            "memory_model": eff.memory_model or eff.model or "gpt-4o-mini",
            "openai_api_key": eff.api_key,
            "openai_base_url": eff.base_url,
            "user_id": user_id,
        })
        mw_list = build_lead_middlewares(
            config,
            config_manager=self.config_manager,
            agent_name=agent_name,
        )
        logger.info("Built %d middlewares for user=%s", len(mw_list), user_id)
        return mw_list

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _init_llm(
        self,
        model: str | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        api_key: str = "",
        base_url: str = "",
    ) -> BaseChatModel:
        # 回退到 contextvar 中的 per-user 凭证 (SubagentManager 调用时模型/凭证均为空)
        creds = _current_req_creds.get()
        model = model or creds.get("model") or "gpt-4o"
        api_key = api_key or creds.get("api_key", "")
        base_url = base_url or creds.get("base_url", "")
        effective_base_url = base_url or "https://api.openai.com/v1"

        # 端到端调试: 输出实际使用的模型和 API 配置
        logger.info(
            "_init_llm: model=%s temperature=%s max_tokens=%s",
            model, temperature, max_tokens,
        )

        if not api_key:
            logger.warning(
                "_init_llm: API Key 为空 — 请在前端「设置」页面配置。"
                " LLM 将不可用, 系统仍可启动但无法执行任务。"
            )
            return ChatOpenAI(
                model=model,
                api_key="MISSING_API_KEY_CONFIGURED",
                base_url=effective_base_url,
                temperature=temperature,
                max_tokens=max_tokens,
                request_timeout=30,
                max_retries=1,
            )

        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=effective_base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout=120,  # 防止请求挂死 (默认 600s 太长)
            max_retries=2,
        )

    async def _cleanup_stale_worktrees(self) -> None:
        """Remove worktrees left over from previous crashes."""
        try:
            from pathlib import Path
            from harness.config.paths import get_paths
            from harness.worktree.manager import GitWorktreeManager

            workspace = str(get_paths().sandbox_work_dir("cleanup"))
            # 确保 workspace 目录存在 (GitWorktreeManager.__init__ 会调用 Path.resolve()，要求路径存在)
            Path(workspace).mkdir(parents=True, exist_ok=True)
            mgr = GitWorktreeManager(workspace)
            removed = await mgr.cleanup_stale()
            if removed:
                logger.info("Cleaned up %d stale worktree(s)", removed)
        except Exception as exc:
            logger.warning("Stale worktree cleanup failed: %s", exc)

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
        env_backend = os.getenv("CHECKPOINTER_BACKEND")
        if env_backend:
            data["backend"] = env_backend
        env_sqlite_dir = os.getenv("CHECKPOINTER_SQLITE_DIR")
        if env_sqlite_dir:
            data["sqlite_dir"] = env_sqlite_dir
        env_pg_url = os.getenv("CHECKPOINTER_POSTGRES_URL")
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

        env_backend = os.getenv("DATABASE_BACKEND")
        if env_backend:
            data["backend"] = env_backend
        env_sqlite_dir = os.getenv("DATABASE_SQLITE_DIR")
        if env_sqlite_dir:
            data["sqlite_dir"] = env_sqlite_dir
        env_pg_url = os.getenv("DATABASE_POSTGRES_URL")
        if env_pg_url:
            data["postgres_url"] = env_pg_url

        return DatabaseConfig(**data) if data else DatabaseConfig()

    # ------------------------------------------------------------------
    # middlewares
    # ------------------------------------------------------------------

    def _bootstrap_check(self, user_id: str) -> None:
        """启动预检 — 检查必需配置, 缺失时引导用户到前端设置页面."""
        import os as _os
        issues: list[str] = []

        # 检查 L1 用户配置中是否设置了 api_key
        from harness.config.config_loader import ConfigLoader
        user_config = ConfigLoader.load_user_global(user_id)
        user_api_key = (user_config or {}).get("api_key", "") if user_config else ""
        env_api_key = _os.getenv("OPENAI_API_KEY", "")

        if not user_api_key and not env_api_key:
            issues.append(
                "API Key 未配置 — LLM 调用将失败\n"
                "  → 请打开前端「设置」页面 → API 配置 → 填入你的 OPENAI_API_KEY"
            )

        if not user_config:
            issues.append(
                "用户全局配置未创建\n"
                "  → 系统将在注册时自动创建, 或手动访问「设置」页面初始化"
            )

        if issues:
            logger.warning("=" * 60)
            logger.warning("配置引导: 检测到 %d 个问题 (用户=%s):", len(issues), user_id)
            for i, msg in enumerate(issues, 1):
                logger.warning("  [%d] %s", i, msg)
            logger.warning("  操作: 打开浏览器 → 设置 → 填入 API Key 即可开始使用")
            logger.warning("=" * 60)

    def _ensure_default_agent(self, user_id: str) -> None:
        """Ensure the 'default' agent exists for the user (idempotent)."""
        from harness.config.agents_config import create_default_agent
        create_default_agent(user_id)

    # ------------------------------------------------------------------
    # Langfuse
    # ------------------------------------------------------------------
    def _build_config(self, thread_id: str, *, public_key: str = "") -> RunnableConfig:
        """Build RunnableConfig with Langfuse callbacks wired in."""
        cfg = RunnableConfig(
            configurable={"thread_id": thread_id},
            recursion_limit=100000,  # 实际不限步数，由 LoopDetectionMiddleware 兜底
        )
        if self.observability and self.observability.enabled and public_key:
            try:
                from langfuse.langchain import CallbackHandler
                handler = CallbackHandler(public_key=public_key)
                cfg["callbacks"] = [handler]
            except Exception as exc:
                logger.warning("Failed to wire Langfuse callback: %s", exc)
        return cfg

    # ------------------------------------------------------------------
    # middleware helpers
    # ------------------------------------------------------------------

    def _get_middleware_by_name(self, name: str) -> Any | None:
        """返回第一个匹配的中间件实例 (遍历所有缓存的 context)."""
        for ctx in self._graph_cache.values():
            for mw in ctx.middlewares:
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
        """通知所有缓存的中间件清理 per-thread 状态."""
        for ctx in self._graph_cache.values():
            for mw in ctx.middlewares:
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
    # team execution
    # ------------------------------------------------------------------

    async def _execute_team(
        self,
        thread_id: str,
        user_id: str,
        message: str,
        project_id: str,
        *,
        effective_config: EffectiveConfig | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Team 模式执行 — 通过 TeamOrchestrator 协调多 Agent 协作.

        Args:
            thread_id: 会话 ID
            user_id: 用户 ID
            message: 用户消息（目标描述）
            project_id: 项目 ID
            effective_config: 当前用户的 EffectiveConfig

        Yields:
            SSE 事件字典
        """
        # ---- 设置 skill 操作的当前用户 ID ----
        from harness.tools.skill_manage_tool import set_skill_user_id
        set_skill_user_id(user_id)

        from harness.team.orchestrator import TeamOrchestrator
        from harness.team.models import TeamMessage, TeamMessageType, TeammateStatus

        # ── 运行时消息注入: 同一 thread 已有活跃 team run → 注入 Lead inbox ──
        existing = self._active_runs.get(thread_id)
        # ── 用户隔离: active_run 属于其他用户 → 不注入/resume, 按无活跃 run 处理 ──
        if (existing and existing.get("user_id")
                and existing.get("user_id") != user_id):
            logger.warning(
                "thread=%s has an active team run owned by another user (%s != %s) — "
                "refusing injection/resume",
                thread_id, existing.get("user_id"), user_id,
            )
            existing = None
        if existing and existing.get("mode") == "team":
            orch = existing.get("orchestrator")
            if orch is not None:
                # ── s32: 澄清恢复 — 用户回答了 Lead 的 ask_clarification ──
                if getattr(orch, "_clarification_pending", False):
                    # ── 已取消的暂停 run → 不走 resume, 清理后落入新建 run ──
                    if existing.get("cancelled"):
                        try:
                            await orch.cancel()
                        except Exception:
                            pass
                        self._active_runs.pop(thread_id, None)
                    else:
                        # ── 澄清暂停 TTL: 懒过期 — 超时则取消并结束 ──
                        paused_at = existing.get("paused_at")
                        if paused_at and (time.time() - paused_at) > CLARIFICATION_TTL_S:
                            logger.info(
                                "Clarification pause expired for thread=%s (>%ds), cancelling",
                                thread_id, CLARIFICATION_TTL_S,
                            )
                            try:
                                await orch.cancel()
                            except Exception:
                                pass
                            self._active_runs.pop(thread_id, None)
                            yield {
                                "type": "team_end",
                                "thread_id": thread_id,
                                "project_id": project_id,
                                "status": "cancelled",
                                "content": "澄清等待超时（30 分钟），团队运行已自动取消。",
                            }
                            return
                        lead = orch._get_lead()
                        if lead is not None and lead.pending_clarification:
                            logger.info(
                                "Resuming team run after clarification: %s",
                                lead.pending_clarification.get("question", "")[:80],
                            )
                            # 恢复执行 → 清除暂停时间戳
                            existing.pop("paused_at", None)
                            # 异常兜底: 此分支在主 try/finally 之外, 必须自行清理 active_run,
                            # 否则残留死 orchestrator 会让后续消息注入死信箱而卡住
                            try:
                                async for event in orch.resume(message):
                                    if self._active_runs.get(thread_id, {}).get("cancelled"):
                                        await orch.cancel()
                                        self._active_runs.pop(thread_id, None)
                                        yield {
                                            "type": "team_end",
                                            "thread_id": thread_id,
                                            "project_id": project_id,
                                            "status": "cancelled",
                                        }
                                        return
                                    yield event
                            except asyncio.CancelledError:
                                await orch.cancel()
                                self._active_runs.pop(thread_id, None)
                                yield {
                                    "type": "team_end",
                                    "thread_id": thread_id,
                                    "project_id": project_id,
                                    "status": "cancelled",
                                }
                                return
                            except Exception as exc:
                                logger.exception(
                                    "Team resume failed for thread=%s", thread_id,
                                )
                                self._active_runs.pop(thread_id, None)
                                yield {
                                    "type": "team_error",
                                    "thread_id": thread_id,
                                    "project_id": project_id,
                                    "content": f"Team 执行异常: {exc}",
                                }
                                return
                            # resume() 返回: 检查是否再次暂停 (clarification) 还是正常结束
                            if orch._clarification_pending:
                                # 再次暂停 → 重新记录暂停时间戳, 不发 finished, 不 pop active_run
                                run_info = self._active_runs.get(thread_id)
                                if run_info is not None:
                                    run_info["paused_at"] = time.time()
                                return
                            # 正常结束 → 清理 active_run, 避免残留已 shutdown 的 orchestrator
                            self._active_runs.pop(thread_id, None)
                            yield {"type": "finished", "thread_id": thread_id}
                            return
                        # Lead 已死/澄清状态丢失 → 清理过期记录, 落入下方新建 run
                        logger.info(
                            "Stale clarification pause for thread=%s (lead unavailable), "
                            "starting new team run",
                            thread_id,
                        )
                        self._active_runs.pop(thread_id, None)

                # ── 普通消息注入: 用户追加需求 → 注入 Lead inbox ──
                lead = orch._get_lead()
                if (lead is not None
                        and lead.status != TeammateStatus.SHUTDOWN):
                    await orch.message_bus.send(TeamMessage(
                        from_agent="user", to_agent=lead.name,
                        msg_type=TeamMessageType.TEXT,
                        content=f"[用户追加需求] {message}",
                    ))
                    yield {
                        "type": "message_injected",
                        "thread_id": thread_id,
                        "project_id": project_id,
                        "content": f"已注入给 Lead ({lead.name})",
                    }
                    return
                # team 已结束 (Lead 已 shutdown) → 清理过期记录, 落入下方新建 run
                logger.info(
                    "Stale active_run for thread=%s (lead dead), starting new team run",
                    thread_id,
                )
                self._active_runs.pop(thread_id, None)

        # ── 并发守卫: 同一项目同时只允许一个 team run (按用户隔离) ──
        # 任务板/信箱是项目级共享文件, 并发 run 会互相取消任务、抢占认领
        for other_tid, info in self._active_runs.items():
            if (other_tid != thread_id and info.get("mode") == "team"
                    and info.get("project_id") == project_id
                    and info.get("user_id") == user_id):
                yield {
                    "type": "team_error",
                    "thread_id": thread_id,
                    "project_id": project_id,
                    "content": (f"项目 {project_id} 已有正在运行的团队任务 "
                                f"(thread={other_tid}), 请等待其完成后再试。"),
                }
                return

        # 注册运行期取消标记
        self._active_runs[thread_id] = {
            "cancelled": False, "mode": "team", "project_id": project_id,
            "user_id": user_id,
        }

        orchestrator: TeamOrchestrator | None = None
        try:
            self._enforce_capacity()

            # 创建并初始化 TeamOrchestrator
            orchestrator = TeamOrchestrator(
                project_id=project_id,
                thread_id=thread_id,
                user_id=user_id,
                llm_factory=self._init_llm,
                tool_registry=self.tool_registry,
                subagent_manager=self.subagent_manager,
                skill_storage=self.skill_storage,
                effective_config=effective_config,
                checkpointer=self._checkpointer,
            )
            await orchestrator.initialize()

            # 保存 orchestrator 引用以支持取消
            self._active_runs[thread_id]["orchestrator"] = orchestrator

            # 执行调度循环
            async for event in orchestrator.run(message):
                # 检查取消标志
                if self._active_runs.get(thread_id, {}).get("cancelled"):
                    await orchestrator.cancel()
                    yield {
                        "type": "team_end",
                        "thread_id": thread_id,
                        "project_id": project_id,
                        "status": "cancelled",
                    }
                    return
                yield event

            # 正常结束 → 补发 finished (暂停等待 clarification 时跳过)
            if not orchestrator or not orchestrator._clarification_pending:
                yield {"type": "finished", "thread_id": thread_id}
            else:
                # ── s32: 澄清暂停 → 记录暂停时间戳 (TTL 懒过期用) ──
                run_info = self._active_runs.get(thread_id)
                if run_info is not None:
                    run_info["paused_at"] = time.time()

        except ValueError as exc:
            # 项目未找到等配置错误 → 降级为单 Agent
            logger.warning("Team init failed, degrading to single-agent: %s", exc)
            yield {
                "type": "team_degrade",
                "thread_id": thread_id,
                "reason": str(exc),
            }
            async for event in self.execute(
                thread_id=thread_id,
                user_id=user_id,
                message=message,
                mode="single",
            ):
                yield event

        except asyncio.CancelledError:
            if orchestrator:
                await orchestrator.cancel()
            yield {
                "type": "team_end",
                "thread_id": thread_id,
                "project_id": project_id,
                "status": "cancelled",
            }

        except Exception as exc:
            logger.exception("Team execution failed for thread=%s", thread_id)
            yield {
                "type": "team_error",
                "thread_id": thread_id,
                "project_id": project_id,
                "content": f"Team 执行异常: {exc}",
            }

        finally:
            # ── s32: 暂停等待 clarification 时不清理, 保留 orchestrator 供下次 resume ──
            run_info = self._active_runs.get(thread_id)
            orch_ref = run_info.get("orchestrator") if run_info else None
            if orch_ref is not None and getattr(orch_ref, "_clarification_pending", False):
                return  # 保留 active_run, 等待用户回答
            self._active_runs.pop(thread_id, None)

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
        project_id: str | None = None,
        agent_name: str = "default",
        mode: str = "single",
        unattended: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute the agent pipeline and stream SSE events in real time.

        When ``mode="team"``, routes to ``_execute_team()`` for multi-agent
        collaboration.

        Args:
            agent_name: 使用的 Agent 配置 (default = 系统默认 agent).
        """
        # ── 校验 agent_name 存在 ──
        agent_name = agent_name or "default"
        if agent_name != "default":
            from harness.config.agents_config import load_agent_config, is_default_agent
            if not is_default_agent(agent_name):
                existing = load_agent_config(agent_name, user_id=user_id)
                if existing is None:
                    yield {
                        "type": "error",
                        "thread_id": thread_id,
                        "content": (
                            f"Agent '{agent_name}' 不存在。"
                            f" 请先通过「Agent 管理」页面创建该 Agent,"
                            f" 或使用默认 Agent (不指定 agent_name)。"
                        ),
                    }
                    return

        if not self._initialized:
            await self.initialize(agent_name=agent_name, user_id=user_id)

        # ── 获取/创建该用户的 GraphContext ──
        ctx = await self._get_or_create_graph_context(user_id, agent_name)
        _review_model = ctx.effective_config.model  # captured for finally block

        # 确保 contextvar 已设置 — 缓存命中时 _build_graph_context 不会执行
        _current_req_creds.set({
            "api_key": ctx.effective_config.api_key,
            "base_url": ctx.effective_config.base_url,
            "model": ctx.effective_config.model,
        })

        # ── 斜杠指令: /compact, /clear (不走 agent 执行, team 路由之前拦截) ──
        cmd = parse_slash_command(message)
        if cmd is not None:
            _cmd_config = self._build_config(
                thread_id, public_key=ctx.effective_config.langfuse_public_key,
            )
            async for event in self._handle_slash_command(
                cmd, thread_id=thread_id, user_id=user_id,
                ctx=ctx, build_config=_cmd_config,
            ):
                yield event
            return

        # ── Team 模式路由 ──
        if mode == "team" and project_id:
            async for event in self._execute_team(
                thread_id=thread_id,
                user_id=user_id,
                message=message,
                project_id=project_id,
                effective_config=ctx.effective_config,
            ):
                yield event
            return

        build_config = self._build_config(
            thread_id, public_key=ctx.effective_config.langfuse_public_key,
        )

        # ── 从 checkpoint 恢复状态（替旧 _active_runs） ──
        try:
            state_snapshot = await ctx.graph.aget_state(build_config)
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

        # ── 无人值守标记：定时任务执行时注入 state.metadata，
        #    cron 工具与 ClarificationMiddleware 据此禁用交互能力（防递归调度/无效追问）。
        #    每次执行都显式覆盖，避免 fixed 会话 checkpoint 中残留旧值。
        metadata = dict(current_state.get("metadata") or {})
        metadata["unattended"] = unattended
        current_state["metadata"] = metadata

        # ── 重连检测: 该 thread 是否有后台 agent task 仍在执行 ──
        existing = self._active_runs.get(thread_id)
        if existing and existing.get("agent_task") and not existing["agent_task"].done():
            _q = existing.get("sse_queue")
            if _q is not None:
                try:
                    while True:
                        event = await _q.get()
                        if event.pop("__sentinel__", False):
                            break
                        try:
                            yield event
                        except (GeneratorExit, asyncio.CancelledError):
                            return  # 客户端又断开了, agent_task 继续跑
                except asyncio.CancelledError:
                    return
                return  # agent_task 已完成, 重连读取完毕

        # ── 注册运行期取消标记（保存 user/agent 以便后续方法查找 ctx） ──
        self._active_runs[thread_id] = {
            "cancelled": False, "user_id": user_id, "agent_name": agent_name,
        }
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
            runner = self._build_custom_graph(graph, graph_context=ctx)
        elif ctx.graph is not None:
            runner = ctx.graph
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
        interrupted = False  # 技能进化器跳过中断的回合

        # ── Per-thread skill evolution counter (跨轮累积) ──
        _skill_iters = self._thread_skill_iters.get(thread_id, 0)
        _called_tool_names: set[str] = set()

        # ── Curator idle tracking ──
        from datetime import datetime, timezone
        self._last_user_activity[user_id] = datetime.now(timezone.utc)

        # ── SubAgent 实时流消费协程 ──
        _subagent_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _drain_subagent_streams():
            """消费所有活跃 SubAgent 的消息队列, 转为 SSE 事件."""
            from harness.agents.subagent_executor import (
                get_subagent_stream,
                list_active_subagent_names,
                remove_subagent_stream,
            )
            while True:
                names = list_active_subagent_names()
                if not names:
                    await asyncio.sleep(0.1)
                    continue

                # 轮询所有活跃队列 — 谁有数据先处理谁
                for name in names:
                    try:
                        stream = get_subagent_stream(name)
                        item = await asyncio.wait_for(stream.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue

                    if item is None:
                        continue

                    msg = item.get("msg", {})
                    msg_type = msg.get("type", "")

                    # ── sentinel: subagent finished, clean up stream ──
                    if msg.get("__sentinel__"):
                        remove_subagent_stream(item["subagent_name"])
                        continue
                    iteration = item.get("iteration", 0)

                    # 根据消息类型构建 SSE 事件
                    if msg_type == "ai":
                        tool_calls = msg.get("tool_calls", [])
                        if tool_calls:
                            for tc in tool_calls:
                                await _subagent_event_queue.put({
                                    "type": "subagent_tool_call",
                                    "thread_id": thread_id,
                                    "subagent_name": item["subagent_name"],
                                    "subagent_task_id": item.get("trace_id", ""),
                                    "tool_name": tc.get("name", "unknown"),
                                    "tool_args": tc.get("args", {}),
                                })
                        content = msg.get("content", "")
                        if content and isinstance(content, str) and content.strip():
                            await _subagent_event_queue.put({
                                "type": "subagent_thinking",
                                "thread_id": thread_id,
                                "subagent_name": item["subagent_name"],
                                "subagent_task_id": item.get("trace_id", ""),
                                "content": content[:2000],
                            })
                    elif msg_type == "tool":
                        await _subagent_event_queue.put({
                            "type": "subagent_tool_result",
                            "thread_id": thread_id,
                            "subagent_name": item["subagent_name"],
                            "subagent_task_id": item.get("trace_id", ""),
                            "tool_name": msg.get("name", "unknown"),
                            "tool_result": str(msg.get("content", ""))[:2000],
                        })

                    # 推送进度
                    await _subagent_event_queue.put({
                        "type": "subagent_progress",
                        "thread_id": thread_id,
                        "subagent_name": item["subagent_name"],
                        "iterations": iteration,
                    })

        # ── 后台 agent task: 解耦执行与 SSE 传输 ──
        # agent 在后台 asyncio.Task 中执行, 通过 sse_queue 与 SSE 生成器通信。
        # 客户端断开时 SSE 生成器退出, 但 agent task 继续执行直到完成。
        sse_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        _client_disconnected = False

        async def _run_agent():
            """后台执行 agent, 将 SSE 事件放入队列。客户端断开不影响此 task。"""
            nonlocal _skill_iters, _title_emitted, _collected_token_usage
            nonlocal active_subagents, final_state, interrupted

            _inner_drainer = asyncio.create_task(_drain_subagent_streams())
            try:
                async for event in runner.astream_events(current_state, build_config, version="v2"):
                    # ── 检查取消标志 ──
                    if self._active_runs.get(thread_id, {}).get("cancelled"):
                        raise asyncio.CancelledError("Execution cancelled by user")

                    # ── 排空 subagent 事件队列 ──
                    while not _subagent_event_queue.empty():
                        try:
                            sub_event = _subagent_event_queue.get_nowait()
                            if sub_event:
                                await sse_queue.put(sub_event)
                        except asyncio.QueueEmpty:
                            break

                    kind = event["event"]
                    evt_name = event.get("name", "")
                    evt_data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]

                    # ── 过滤子代理内部事件 ──
                    _event_tags: list[str] = event.get("tags", []) or []
                    if any(t.startswith("subagent:") for t in _event_tags):
                        continue

                    # ── Token-level streaming ──
                    if kind == "on_chat_model_stream":
                        chunk: Any = evt_data.get("chunk")
                        if chunk is None:
                            continue
                        reasoning = (
                            getattr(chunk, "reasoning_content", None)
                            or getattr(chunk, "additional_kwargs", {}).get("reasoning_content", "")
                        )
                        if reasoning and isinstance(reasoning, str):
                            await sse_queue.put({
                                "type": "thinking", "content": reasoning, "thread_id": thread_id,
                            })
                        content = getattr(chunk, "content", "")
                        if not content:
                            continue
                        if isinstance(content, list):
                            content = "".join(
                                c.get("text", "") if isinstance(c, dict) else str(c)
                                for c in content
                            )
                        if content:
                            await sse_queue.put({
                                "type": "message", "content": str(content), "thread_id": thread_id,
                            })

                    # ── Token usage ──
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
                            await sse_queue.put({
                                "type": "token_usage", "thread_id": thread_id,
                                "tokens": usage.model_dump(),
                            })

                    # ── Tool start ──
                    elif kind == "on_tool_start":
                        tool_name = evt_name
                        _skill_iters += 1
                        if tool_name == "skill_manage":
                            _skill_iters = 0
                        tool_input: Any = evt_data.get("input", {})
                        run_id_s: str = event.get("run_id", "")
                        if tool_name in ("task", "Agent") and isinstance(tool_input, dict):
                            sub_name = tool_input.get("name") or tool_input.get("agent_name", "unknown")
                            active_subagents[run_id_s] = str(sub_name)
                            _max_turns = 50
                            if self.subagent_manager is not None:
                                _cfg = self.subagent_manager.get(str(sub_name))
                                if _cfg is not None:
                                    _max_turns = getattr(_cfg, "max_turns", 50)
                            await sse_queue.put({
                                "type": "subagent_start", "thread_id": thread_id,
                                "subagent_name": str(sub_name),
                                "instruction": str(tool_input.get("instruction", "")),
                                "context": str(tool_input.get("context", "")),
                                "max_turns": _max_turns,
                            })
                        elif tool_name not in ("task", "Agent"):
                            await sse_queue.put({
                                "type": "tool_call", "thread_id": thread_id,
                                "tool_name": tool_name,
                                "tool_args": tool_input if isinstance(tool_input, dict) else {"input": str(tool_input)},
                            })

                    # ── Tool end ──
                    elif kind == "on_tool_end":
                        tool_name = evt_name
                        run_id_s = event.get("run_id", "")
                        tool_output: Any = evt_data.get("output", "")
                        if tool_name in ("task", "Agent"):
                            sub_name = active_subagents.pop(run_id_s, "unknown")
                            subagent_result: dict[str, Any] | None = None
                            output_str = ""
                            if hasattr(tool_output, "content"):
                                output_str = str(tool_output.content)
                            elif isinstance(tool_output, str):
                                output_str = tool_output
                            if self.subagent_manager is not None:
                                full_result = self.subagent_manager.pop_last_result(sub_name)
                                if full_result is not None:
                                    subagent_result = full_result.model_dump()
                            summary = output_str[:2000] if output_str else ""
                            if subagent_result:
                                if not summary:
                                    summary = subagent_result.get("output", "") or ""
                                if subagent_result.get("error"):
                                    summary = f"[{subagent_result['status']}] {subagent_result['error']}"
                            await sse_queue.put({
                                "type": "subagent_end", "thread_id": thread_id,
                                "subagent_name": sub_name, "content": summary[:2000],
                                "status": subagent_result.get("status", "success") if subagent_result else "done",
                                "subagent_result": subagent_result,
                                "duration_ms": (
                                    int((datetime.now(timezone.utc) - datetime.fromisoformat(subagent_result["started_at"])).total_seconds() * 1000)
                                    if subagent_result and subagent_result.get("started_at") else None
                                ),
                            })
                        elif tool_name not in ("task", "Agent"):
                            output_str = ""
                            if hasattr(tool_output, "content"):
                                output_str = str(tool_output.content)
                            elif isinstance(tool_output, str):
                                output_str = tool_output
                            else:
                                output_str = str(tool_output)
                            await sse_queue.put({
                                "type": "tool_result", "thread_id": thread_id,
                                "tool_name": tool_name, "tool_result": output_str[:2000],
                            })

                    # ── Root graph completion ──
                    elif kind == "on_chain_end" and evt_name == "LangGraph":
                        result: Any = evt_data.get("output")
                        if isinstance(result, dict):
                            final_state = result

                # ── Post-stream ──
                if _collected_token_usage and self.observability:
                    self.observability.log_token_usage(trace_id, "", _collected_token_usage)

                suggested_title = final_state.get("suggested_title")
                if suggested_title and not _title_emitted:
                    _title_emitted = True
                    await sse_queue.put({
                        "type": "title_update", "thread_id": thread_id, "title": suggested_title,
                    })

                pending_clarification = get_pending_clarification(
                    final_state.get("messages", [])
                )
                if pending_clarification:
                    if self._run_store:
                        completion = journal.get_completion_data() if journal else {}
                        await self._run_store.update_run_completion(run_id, "pending", **completion)
                    await sse_queue.put({
                        "type": "clarification", "request": pending_clarification, "thread_id": thread_id,
                    })
                    if self.observability:
                        self.observability.finalize_trace(trace_id, "suspended")
                    await sse_queue.put({"__sentinel__": True, "__clarification__": True})
                    return

                # ── Run success ──
                if self._run_store:
                    completion = journal.get_completion_data() if journal else {}
                    await self._run_store.update_run_completion(run_id, "success", **completion)
                await sse_queue.put({"type": "finished", "thread_id": thread_id, "run_id": run_id})
                if self.observability:
                    self.observability.finalize_trace(trace_id, "success")
                await sse_queue.put({"__sentinel__": True})

            except asyncio.CancelledError:
                interrupted = True
                if self._run_store:
                    await self._run_store.update_status(run_id, "interrupted")
                await sse_queue.put({"type": "error", "content": "执行已取消", "thread_id": thread_id})
                if self.observability:
                    self.observability.finalize_trace(trace_id, "cancelled")
                await sse_queue.put({"__sentinel__": True})
            except Exception as exc:
                interrupted = True
                logger.exception("Execution failed for thread=%s", thread_id)
                if self._run_store:
                    await self._run_store.update_status(run_id, "error", error=str(exc))
                await sse_queue.put({"type": "error", "content": str(exc), "thread_id": thread_id})
                if self.observability:
                    self.observability.finalize_trace(trace_id, "error")
                await sse_queue.put({"__sentinel__": True})
            finally:
                _inner_drainer.cancel()
                try:
                    await _inner_drainer
                except asyncio.CancelledError:
                    pass
                # 排空残留 subagent 事件
                while not _subagent_event_queue.empty():
                    try:
                        sub_event = _subagent_event_queue.get_nowait()
                        if sub_event:
                            await sse_queue.put(sub_event)
                    except asyncio.QueueEmpty:
                        break
                if journal:
                    await journal.flush()

        agent_task = asyncio.create_task(_run_agent())
        self._active_runs[thread_id]["agent_task"] = agent_task
        self._active_runs[thread_id]["sse_queue"] = sse_queue

        try:
            while True:
                event = await sse_queue.get()
                if event.pop("__sentinel__", False):
                    _has_clarification = event.pop("__clarification__", False)
                    break
                try:
                    yield event
                except (GeneratorExit, asyncio.CancelledError):
                    _client_disconnected = True
                    # agent_task 继续后台执行, 不再 yield 但仍从队列消费
                    while True:
                        evt = await sse_queue.get()
                        if evt.pop("__sentinel__", False):
                            break

        except GeneratorExit:
            _client_disconnected = True
        except asyncio.CancelledError:
            _client_disconnected = True
        except Exception as exc:
            interrupted = True
            logger.exception("SSE streaming failed for thread=%s", thread_id)
            if self._run_store:
                await self._run_store.update_status(run_id, "error", error=str(exc))
            yield {"type": "error", "content": str(exc), "thread_id": thread_id}
            if self.observability:
                self.observability.finalize_trace(trace_id, "error")
        finally:
            if _client_disconnected:
                # agent_task 仍在后台运行 → 注册完成回调自动清理
                def _on_agent_done(t: asyncio.Task) -> None:
                    self._active_runs.pop(thread_id, None)
                    self._thread_skill_iters.pop(thread_id, None)
                    self._cleanup_middleware_state(thread_id)
                agent_task.add_done_callback(_on_agent_done)
            else:
                # 正常完成 → 等待 agent_task 结束, 然后清理
                if not agent_task.done():
                    await agent_task
                drainer_task_ref = None  # drainer 已在 _run_agent 内管理
                has_pending_clarification = get_pending_clarification(
                    final_state.get("messages", [])
                ) is not None if final_state else False
                if not has_pending_clarification:
                    self._cleanup_middleware_state(thread_id)
                # ── 后台技能 review ──
                if (
                    self._skill_nudge_interval > 0
                    and _skill_iters >= self._skill_nudge_interval
                    and final_state
                    and not interrupted
                ):
                    final_messages = final_state.get("messages", [])
                    if final_messages:
                        try:
                            from harness.skills.evolution.review_fork import spawn_background_review
                            _enabled_skills = self.skill_storage.load_skills(
                                enabled_only=True, user_id=user_id,
                            ) if self.skill_storage else []
                            _memory_context = _get_memory_context(user_id)
                            asyncio.create_task(
                                spawn_background_review(
                                    messages_snapshot=list(final_messages),
                                    skill_storage=self.skill_storage,
                                    llm_factory=self._init_llm,
                                    model=_review_model,
                                    user_id=user_id,
                                    enabled_skills=_enabled_skills,
                                    memory_context=_memory_context,
                                )
                            )
                            logger.info(
                                "Background skill review scheduled (thread=%s, iters=%d)",
                                thread_id, _skill_iters,
                            )
                        except Exception:
                            pass
                    self._thread_skill_iters[thread_id] = 0
                else:
                    self._thread_skill_iters[thread_id] = _skill_iters
                if not has_pending_clarification:
                    self._active_runs.pop(thread_id, None)
                    # 计数器跨轮累积, 不在此清理 — 仅在达标归零 (line 1517) 或
                    # 客户端断开回调 (line 1472) 时重置.


    async def _handle_slash_command(
        self,
        cmd: str,
        *,
        thread_id: str,
        user_id: str,
        ctx: GraphContext,
        build_config: dict,
    ) -> AsyncIterator[dict[str, Any]]:
        """处理 /compact 与 /clear 斜杠指令 (不走 agent 执行)."""

        def _reply(text: str) -> list[dict[str, Any]]:
            return [
                {"type": "message", "content": text, "thread_id": thread_id},
                {"type": "finished", "thread_id": thread_id},
            ]

        # 活跃运行中禁止操作 (压缩/清空会与正在写入的 checkpoint 冲突)
        existing = self._active_runs.get(thread_id)
        if existing and existing.get("agent_task") and not existing["agent_task"].done():
            for e in _reply("当前任务正在执行中，请先停止或等待完成后再操作。"):
                yield e
            return

        if cmd == "/clear":
            # 只清 checkpoint + 中间件状态, 不删工作区文件 (区别于 delete_thread)
            if self._checkpointer is not None:
                try:
                    await self._checkpointer.adelete_thread(thread_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to delete checkpoint for thread %s: %s",
                        thread_id, exc,
                    )
            self._cleanup_middleware_state(thread_id)
            self._thread_skill_iters.pop(thread_id, None)
            self._active_runs.pop(thread_id, None)
            logger.info("/clear executed (thread=%s, user=%s)", thread_id, user_id)
            # 通知前端清空本地状态; App DB 历史消息保留不删
            yield {"type": "context_cleared", "thread_id": thread_id}
            for e in _reply("上下文已清空。下一轮消息将开始全新会话（工作区文件保留）。"):
                yield e
            return

        # /compact — 跳过阈值, 强制对当前历史执行一次摘要压缩
        try:
            snapshot = await ctx.graph.aget_state(build_config)
        except Exception:
            logger.exception("aget_state failed for /compact (thread=%s)", thread_id)
            snapshot = None
        messages = (
            list(snapshot.values.get("messages", []))
            if snapshot and snapshot.values else []
        )
        if not messages:
            for e in _reply("当前没有可压缩的上下文。"):
                yield e
            return

        from harness.middleware.summarization import SummarizationMiddleware
        summ_mw = next(
            (m for m in ctx.middlewares if isinstance(m, SummarizationMiddleware)),
            None,
        )
        if summ_mw is None:
            for e in _reply("压缩功能未启用（summarization.enabled=false）。"):
                yield e
            return

        update = await summ_mw.aforce_summarize(messages)
        if update is None:
            for e in _reply("上下文太短，无需压缩。"):
                yield e
            return

        await ctx.graph.aupdate_state(build_config, update)
        after = len(update["messages"]) - 1  # 去掉 RemoveMessage 占位
        logger.info(
            "/compact executed (thread=%s, user=%s, %d -> %d messages)",
            thread_id, user_id, len(messages), after,
        )
        # 摘要消息 (name="summary") 对 UI 隐藏且不入 app DB — 在回复中回显,
        # 同时让 "0.0s 完成" 这类异常 (错误/降级文案) 对用户可自诊断.
        summary_text = str(getattr(update["messages"][1], "content", "") or "")
        preview = summary_text[:2000] + ("…" if len(summary_text) > 2000 else "")
        for e in _reply(
            f"上下文已压缩：{len(messages)} 条消息 → {after} 条（历史摘要 + 保留尾部）。"
            f"\n\n{preview}"
        ):
            yield e

    async def respond_to_clarification(
        self,
        thread_id: str,
        answer: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Resume execution after a clarification answer, streaming results.

        DeerFlow-style message-based HITL: the user's answer is appended as a
        regular ``HumanMessage`` and the graph resumes from the checkpoint. No
        custom ``pending_clarification`` state key is used; pending status is
        inferred directly from the message history.
        """
        # 从 _active_runs 获取 user/agent, 默认为 default
        run_info = self._active_runs.get(thread_id, {})
        user_id = run_info.get("user_id", "default")
        agent_name = run_info.get("agent_name", "default")
        ctx = await self._get_or_create_graph_context(user_id, agent_name)

        build_config = self._build_config(
            thread_id, public_key=ctx.effective_config.langfuse_public_key,
        )

        # ── 从 checkpoint 读取暂停状态 ──
        try:
            state_snapshot = await ctx.graph.aget_state(build_config)
        except Exception:
            logger.exception("aget_state failed for thread=%s", thread_id)
            yield {"type": "error", "content": f"thread '{thread_id}' not found", "thread_id": thread_id}
            return

        if state_snapshot is None or not state_snapshot.values:
            yield {"type": "error", "content": f"thread '{thread_id}' not active", "thread_id": thread_id}
            return

        state = dict(state_snapshot.values)
        messages = list(state.get("messages", []))

        # Verify the conversation is actually waiting for a clarification.
        pending = get_pending_clarification(messages)
        if pending is None:
            yield {"type": "error", "content": "no pending clarification", "thread_id": thread_id}
            return

        # Inject the user's answer into the message history so the model sees it
        # when execution resumes. Without this, the model only sees its own
        # clarification question and will ask again.
        messages.append(HumanMessage(content=answer))
        state["messages"] = messages

        # ── 注册运行期取消标记 ──
        self._active_runs[thread_id] = {"cancelled": False}

        # Emit start event
        yield {"type": "message", "content": "", "thread_id": thread_id, "status": "resumed"}

        final_state: dict[str, Any] = {}
        try:
            async for event in ctx.graph.astream_events(state, build_config, version="v2"):
                kind = event["event"]
                evt_name = event.get("name", "")
                evt_data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]

                # ── 过滤子代理内部事件 ──
                _tags: list[str] = event.get("tags", []) or []
                if any(t.startswith("subagent:") for t in _tags):
                    continue

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
                    if tool_name in ("task", "Agent"):
                        sub_name = (
                            (tool_input.get("name") or tool_input.get("agent_name", "unknown"))
                            if isinstance(tool_input, dict) else "unknown"
                        )
                        yield {
                            "type": "subagent_start", "thread_id": thread_id,
                            "subagent_name": str(sub_name),
                            "instruction": str(tool_input.get("instruction", "")) if isinstance(tool_input, dict) else "",
                            "context": str(tool_input.get("context", "")) if isinstance(tool_input, dict) else "",
                        }
                    elif tool_name not in ("task", "Agent"):
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

            # Post-stream: check for new clarifications from the message history.
            new_pending = get_pending_clarification(final_state.get("messages", []))
            if new_pending is not None:
                yield {
                    "type": "clarification",
                    "request": new_pending,
                    "thread_id": thread_id,
                }
                return

            # Success — clean up runtime flag only (checkpoint persists)
            self._active_runs.pop(thread_id, None)
            self._cleanup_middleware_state(thread_id)
            yield {"type": "finished", "thread_id": thread_id}

        except Exception as exc:
            logger.exception("Resumption failed for thread=%s", thread_id)
            self._active_runs.pop(thread_id, None)
            yield {"type": "error", "content": str(exc), "thread_id": thread_id}

    async def stop(self, thread_id: str) -> None:
        """Cancel a running execution.

        Only sets the cancelled flag on the runtime marker — checkpoint
        state is preserved so the user can resume later if needed.

        In team mode, also cancels the TeamOrchestrator.
        """
        run = self._active_runs.get(thread_id)
        if run is not None:
            run["cancelled"] = True
            # ── Team 模式: 取消 TeamOrchestrator ──
            orchestrator = run.get("orchestrator")
            if orchestrator is not None:
                try:
                    await orchestrator.cancel()
                except Exception:
                    pass
                # cancel 后移除 active_run — 避免残留死 orchestrator
                # 让下一条消息注入死信箱 (澄清暂停时没有运行中的循环来消费 cancelled 标记)
                self._active_runs.pop(thread_id, None)

    async def get_status(self, thread_id: str) -> dict[str, Any]:
        """Return thread execution status, reading from LangGraph checkpoint."""
        # 从 _active_runs 获取 user/agent, 默认使用 default
        run_info = self._active_runs.get(thread_id, {})
        # 有活跃 run → 直接按运行态返回.
        # 对 team 线程尤其重要: 其 checkpoint 写在 team-{pid}-{tid}-{agent} 下,
        # 主 graph 查不到, 会继续往下误报 inactive
        if run_info:
            return {
                "thread_id": thread_id,
                "status": "cancelling" if run_info.get("cancelled") else "running",
            }
        user_id = run_info.get("user_id", "default")
        agent_name = run_info.get("agent_name", "default")
        ctx = await self._get_or_create_graph_context(user_id, agent_name)

        build_config = self._build_config(
            thread_id, public_key=ctx.effective_config.langfuse_public_key,
        )

        try:
            state_snapshot = await ctx.graph.aget_state(build_config)
        except Exception:
            logger.warning("aget_state failed for thread=%s", thread_id)
            return {"thread_id": thread_id, "status": "inactive"}

        if state_snapshot is None or not state_snapshot.values:
            return {"thread_id": thread_id, "status": "inactive"}

        state = state_snapshot.values
        messages = state.get("messages", [])
        is_pending = get_pending_clarification(messages) is not None

        # Check runtime cancelled marker
        active_run = self._active_runs.get(thread_id)
        if active_run and active_run.get("cancelled"):
            return {"thread_id": thread_id, "status": "cancelling"}

        return {
            "thread_id": thread_id,
            "status": "suspended" if is_pending else "running",
        }

    # ------------------------------------------------------------------
    # custom graph builder (frontend canvas support)
    # ------------------------------------------------------------------

    def _build_custom_graph(
        self, execution_graph: ExecutionGraph, *, graph_context: GraphContext | None = None,
    ) -> Any:
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
        # 捕获当前 ctx 的 graph, 用于 lead node
        _ctx_graph = graph_context.graph if graph_context else None

        # Add a node for each agent in the canvas
        for node in execution_graph.nodes:
            node_id = node.id
            if node.type == "lead":
                # Lead agent node — uses the full agent subgraph (create_agent)
                async def _lead_node(
                    state: HarnessState,
                    _node_id: str = node_id,  # 默认参数捕获，防止闭包共享
                ) -> HarnessState:
                    if _ctx_graph is None:
                        return state
                    from langgraph.config import get_config
                    cfg = get_config()
                    return await _ctx_graph.nodes["agent"].ainvoke(state, cfg)
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
    yaml_path = Path(__file__).resolve().parent / "config.yaml"
    config_mgr: ConfigManager | None = None
    if yaml_path.exists():
        config_mgr = ConfigManager(config_path=str(yaml_path))
        config_mgr.load()
        logger.info("Loaded config.yaml from %s", yaml_path)

    harness = HarnessService(config, config_manager=config_mgr)
    await harness.initialize(agent_name="default", user_id="default")
    set_harness(harness)

    config_obj = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=config.port,
        log_level="info",
    )
    server = uvicorn.Server(config_obj)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
