"""TeamOrchestrator — 事件驱动的多 Agent 编排器.

核心流程:
    PLANNING → SPAWN → EVENT_LOOP → SYNTHESIS → COMPLETED

与旧版关键差异:
- 用 TeammateAgent (持久化, 有自己 agent loop)
- 事件驱动唤醒替代 sleep() 忙等轮询
- Lead 在 event loop 中持续参与, 可动态 spawn / shutdown (握手) teammate
- PHASE 2 不再是 blind dispatch, 而是 Lead agent 持续 ReAct 决策
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from langchain_core.messages import HumanMessage

from harness.config.paths import get_paths
from harness.team.context import TeamContext
from harness.team.message_bus import TeamMessageBus
from harness.team.models import (
    TeamMemberRuntime,
    TeamMessage,
    TeamMessageType,
    TeamTask,
    TeamTaskStatus,
    TeammateStatus,
)
from harness.memory.task_memory import TaskMemoryStore
from harness.memory.team_memory import TeamMemoryStore
from harness.memory.member_memory import MemberMemoryStore, extract_lessons_from_task
from harness.observability.team_tracer import TeamTracer
from harness.team.task_store import TeamTaskStore
from harness.team.teammate_agent import TeammateAgent
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── 常量 ──
OVERALL_TIMEOUT = 1800         # Team 整体超时 30 分钟
DEADLOCK_TIMEOUT = 300         # 死锁 5 分钟无进展

# 平台内置 Lead Agent 的保留名称 (双下划线前缀防止与用户 agent 冲突)
TEAM_LEAD_NAME = "__team_lead__"

# 平台内置 Verifier Agent 的保留名称 — 独立验收者 (执行者不得验收自己的产出),
# 高危任务的验收子任务由它执行; 不属于项目 members 列表, 按需懒加载 (_ensure_verifier)
TEAM_VERIFIER_NAME = "__team_verifier__"

# Lead Agent 允许加载的 tool_groups 白名单 — Lead 是纯协调者:
# 执行类工具 (files 写入/sandbox/code) 一律不授予, 强制 Lead 只做意图识别+任务分发;
# 只读文件 (files_readonly) 与搜索保留, 用于 triage/synthesis 阶段的上下文了解。
LEAD_ALLOWED_TOOL_GROUPS = {"files_readonly", "search"}

# Verifier 允许加载的 tool_groups 白名单 — 验收需要读文件/查资料核对证据,
# 不授予写入类工具 (验收者不应修改交付物)
VERIFIER_ALLOWED_TOOL_GROUPS = {"files_readonly", "search"}

# 领域匹配认领阈值 — compute_card_task_match 的评分尺度:
# 工具 +25/个, 技能 +30/个, CJK bigram +3/个 (纯中文任务有效匹配约 15+)
CLAIM_THRESHOLD = 15

# ── Phase 3: Verifier 验收结论解析 ──
_VERDICT_RE = re.compile(r"VERDICT\s*[:：]\s*(PASS|FAIL)\b", re.IGNORECASE)


def _parse_verdict(text: str) -> tuple[str | None, str]:
    """解析 Verifier 输出中的 VERDICT 行. 返回 ("pass"|"fail"|None, 理由).

    解析失败 (无 VERDICT 行) 返回 (None, "") — 调用方按 fail-safe 转 Lead 审查。
    """
    if not text:
        return None, ""
    m = _VERDICT_RE.search(text)
    if not m:
        return None, ""
    verdict = m.group(1).lower()
    # 理由: VERDICT 行内剩余文本 + 后续行 (去前缀分隔符)
    reason = text[m.end():].strip().lstrip("—-:： ").strip()
    if not reason:
        # 理由写在 VERDICT 行之前的情况: 取前文最后一段
        reason = text[:m.start()].strip().splitlines()[-1].strip() if text[:m.start()].strip() else ""
    return verdict, reason[:500]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_project_json(project_id: str, user_id: str) -> dict[str, Any] | None:
    """加载项目 JSON 文件 (兼容新旧两种存储格式).

    新格式: projects/{pid}/project.json
    旧格式: projects/{pid}.json  → 自动迁移到新格式
    """
    paths = get_paths()
    new_path = paths.base_dir / "users" / user_id / "projects" / project_id / "project.json"
    old_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"

    if new_path.exists():
        with open(new_path) as f:
            return json.load(f)

    # ── 向后兼容: 旧格式自动迁移 ──
    if old_path.exists():
        logger.info("Migrating project '%s' from old format → new format", project_id)
        with open(old_path) as f:
            project = json.load(f)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        with open(new_path, "w") as f:
            json.dump(project, f, indent=2, ensure_ascii=False)
        old_path.unlink()
        return project

    return None


class TeamOrchestrator:
    """事件驱动的多 Agent 编排器.

    使用方式:
        orchestrator = TeamOrchestrator(...)
        await orchestrator.initialize()
        async for event in orchestrator.run(message):
            yield SSE events
    """

    def __init__(
        self,
        project_id: str,
        thread_id: str,
        user_id: str,
        *,
        llm_factory: Any = None,
        tool_registry: ToolRegistry | None = None,
        subagent_manager: Any = None,
        skill_storage: Any = None,
        effective_config: Any = None,
        checkpointer: Any = None,  # LangGraph BaseCheckpointSaver
    ) -> None:
        self._project_id = project_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._subagent_manager = subagent_manager
        self._skill_storage = skill_storage
        self._effective_config = effective_config  # Lead's EffectiveConfig
        self._checkpointer = checkpointer

        # ── 核心组件 ──
        self.task_store = TeamTaskStore(project_id, user_id, thread_id)
        self.message_bus = TeamMessageBus(project_id, user_id, thread_id)
        self._task_memory_store = TaskMemoryStore(project_id, user_id)
        self._team_memory_store = TeamMemoryStore(project_id, user_id)
        self._member_memory_store = MemberMemoryStore(user_id)
        self.team_context: TeamContext | None = None
        self.teammates: dict[str, TeammateAgent] = {}  # agent_name → TeammateAgent (仅已 spawn)
        # ── 成员名册 (懒加载): initialize 时只存名字不 spawn, 首次派单/
        # 恢复任务时由 _ensure_teammate 按需拉起, 避免简单问候也拉起全部成员 ──
        self._member_names: list[str] = []

        # ── 调度状态 ──
        self._round: int = 0
        self._cancelled: bool = False
        self._clarification_pending: bool = False
        self._started_at: str = ""
        self._last_progress_at: str = ""
        self._progress_event = asyncio.Event()
        # run 开始时已处终态的历史任务 id — synthesis 只汇总本 run 的产出,
        # 避免项目级持久任务板上的旧结果被当作本次回答输出
        self._stale_terminal_ids: set[str] = set()

        # ── LLM 并发控制: 限制同时调用 LLM 的 member 数量 ──
        # 注: max_concurrent_subagents 是 SubAgent 并发数 (默认 3), 与 Member LLM 并发无关
        # 这里使用独立的硬编码默认值, 避免错误的配置耦合
        self._llm_semaphore = asyncio.Semaphore(5)  # 最多 5 个 member 同时调用 LLM

        # ── SSE 事件队列 ──
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # ── Tracing ──
        from harness.config.paths import get_paths
        trace_dir = get_paths().base_dir / "users" / user_id / "projects" / project_id / "traces" / thread_id
        langfuse_public_key = ""
        langfuse_secret_key = ""
        langfuse_host = ""
        langfuse_enabled = False
        if effective_config is not None:
            langfuse_enabled = effective_config.langfuse_enabled
            langfuse_public_key = effective_config.langfuse_public_key or ""
            langfuse_secret_key = effective_config.langfuse_secret_key or ""
            langfuse_host = effective_config.langfuse_host or ""
        self.tracer = TeamTracer(
            trace_dir=trace_dir,
            session_id=thread_id,
            user_id=user_id,
            public_key=langfuse_public_key or None,
            secret_key=langfuse_secret_key or None,
            host=langfuse_host or None,
            enabled=langfuse_enabled,
        )

        # ── 去重: 已通知过 Lead 的空闲 teammate ──
        self._notified_idle: set[str] = set()

        # ── Phase 3: Verifier 验收 ──
        self._verifier_name: str | None = None   # 检测到的 Verifier 成员名 (缓存)
        self._verifier_checked: bool = False     # 是否已检测过 (动态 spawn 新成员时重置)
        self._verdict_processed: set[str] = set()  # 已消化过 VERDICT 的验收子任务 id

        # ── Phase 6: worktree 隔离成员 (agent_name → WorktreeContext) ──
        # 改动保留不 merge, run 结束 log 提示路径, 项目删除时按登记清单回收
        self._worktree_contexts: dict[str, Any] = {}

    @property
    def _roster(self) -> list[str]:
        """成员名册 (懒加载的名字列表; 测试经 __new__ 绕过 __init__ 时回退空名册)."""
        return getattr(self, "_member_names", [])

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """加载项目、生成 agent cards、创建 Lead Agent (member 懒加载, 不全量 spawn).

        流程:
        1. 加载项目 JSON → 获取成员名册 (纯执行者, 只登记名字, 不 spawn)
        2. 为所有成员生成 agent-card.json — 注入 TeamContext
           (Lead 感知成员能力的来源, 与成员是否已 spawn 无关)
        3. 创建平台内置 Lead Agent (TEAM_LEAD_NAME, default 配置 + 系统 SOUL)
        4. Member 不在此 spawn — 首次派单/恢复任务时由 _ensure_teammate
           按需拉起 (lazy spawn), 避免 "您好" 这类自处理场景也拉起全部成员
        """
        project = await _load_project_json(self._project_id, self._user_id)
        if project is None:
            raise ValueError(f"Project '{self._project_id}' not found")

        project_name = project.get("name", self._project_id)
        project_description = project.get("description", "")
        member_names: list[str] = project.get("members", [])
        self._member_names = member_names

        if not member_names:
            logger.warning("Project '%s' has no members — team mode will degrade", self._project_id)

        lead_name = TEAM_LEAD_NAME

        # ── 1. 构建 TeamMemberRuntime 列表 (Lead 排第一, 所有 member 都是 worker) ──
        member_runtimes: list[TeamMemberRuntime] = []
        # Lead 先添加
        member_runtimes.append(TeamMemberRuntime(
            agent_name=lead_name, role="lead", status=TeammateStatus.SPAWNING,
        ))
        for name in member_names:
            member_runtimes.append(TeamMemberRuntime(
                agent_name=name, role="member", status=TeammateStatus.SPAWNING,
            ))

        self.team_context = TeamContext(
            project_id=self._project_id,
            project_name=project_name,
            project_description=project_description,
            user_id=self._user_id,
            lead_name=lead_name,
            members=member_runtimes,
        )

        # ── 2. 生成 agent cards (mtime 缓存: 配置未变则复用, 避免重复扫描) ──
        from harness.team.agent_card import (
            generate_agent_card, save_project_cards, is_card_stale, get_card,
        )
        from harness.config.config_loader import ConfigLoader as _CL

        cards: dict[str, Any] = {}
        stale_count = 0
        for name in member_names:
            try:
                if not is_card_stale(self._project_id, name, user_id=self._user_id):
                    cached = get_card(self._project_id, name, user_id=self._user_id)
                    if cached is not None:
                        cards[name] = cached
                        continue

                stale_count += 1
                member_eff = _CL.load_effective(user_id=self._user_id, agent_name=name)
                card = generate_agent_card(
                    name,
                    user_id=self._user_id,
                    tool_registry=self._tool_registry,
                    skill_storage=self._skill_storage,
                    effective_config=member_eff,
                    role="member",
                )
                cards[name] = card
            except Exception as exc:
                logger.warning("Failed to generate agent card for '%s': %s", name, exc)

        if stale_count > 0:
            logger.info("Agent cards: %d/%d regenerated, %d from cache",
                        stale_count, len(member_names), len(member_names) - stale_count)

        # 全量写入单个 agent_card.json (有更新时才写, 但写操作本身轻量)
        if cards:
            save_project_cards(self._project_id, cards, user_id=self._user_id)

        # 注入 TeamContext — 后续 TeammateAgent 构建 system prompt 时可用
        if cards:
            self.team_context.set_team_capabilities(cards)
            logger.info("Agent cards generated: %d members", len(cards))
        else:
            logger.warning("No agent cards generated — team capabilities unavailable")

        # ── 3. 创建平台内置 Lead Agent ──
        lead = await self._create_lead()
        if lead is None:
            # Lead 是平台基础设施 (task_review / delegate_to_member 等仅 Lead 持有),
            # 缺失时 IN_REVIEW 无人审查、派单失效 → 明确报错 (上层降级为单 Agent)
            raise ValueError(
                "Team 初始化失败: 平台 Lead Agent 无法创建。"
                " 请检查 default agent 的配置 (model/api_key) 是否正确。"
            )

        # ── 4. Member 懒加载: 不在此全量 spawn ──
        # 成员首次被派单 / 恢复中断任务 / Lead spawn_teammate 时, 由
        # _ensure_teammate 按需拉起; 成员创建失败也只影响对应任务的分派,
        # 不再阻塞整个 team 初始化
        logger.info(
            "TeamOrchestrator initialized: project=%s teammates=1 lead spawned, "
            "%d members standby (lazy spawn) lead=%s",
            self._project_id, len(member_names), lead_name,
        )

        # ── 5. 团队成员快照保鲜 (名册 + 活体合并, 未 spawn 成员以 IDLE 占位) ──
        self._refresh_team_context()

        # ── 6. 回收无主 IN_PROGRESS 任务 (上一个 run crash 后的遗留) ──
        orphaned = await self.task_store.recover_orphaned_tasks()
        if orphaned:
            logger.info("Recovered %d orphaned tasks from previous crashed run", len(orphaned))

        # ── 7. 加载团队记忆 (L3) ──
        try:
            team_memory_xml = await self._team_memory_store.get_context_xml()
            if team_memory_xml:
                self.team_context.set_team_memory_xml(team_memory_xml)
                logger.info("Team memory loaded for project %s", self._project_id)
        except Exception as exc:
            logger.warning("Failed to load team memory: %s", exc)

        # ── 8. 前端兼容: 未 spawn 成员补发 member_status(idle) ──
        # 前端 team_start 的 initMembers 初始状态为 spawning; 懒加载下成员不再
        # 经历 init spawn, 补发 idle 避免 Members 标签一直停留在 "spawning".
        # 事件入队后在 run() 的 team_start 之后被 drain, 顺序安全
        for name in self._member_names:
            if name not in self.teammates:
                await self._event_queue.put(await self._emit_member_status(name, "idle"))

    async def _ensure_teammate(self, name: str) -> TeammateAgent | None:
        """按需拉起名册成员 (lazy spawn) — 已存活直接返回, 未 spawn 才创建.

        team 成员不在 initialize() 全量 spawn, 首次派单/恢复任务/Lead 主动
        spawn 时由这里按需拉起; spawn 成功后刷新 TeamContext 快照并补发
        member_status 事件. 拉起失败 (配置缺失/LLM 不可用) 返回 None.
        """
        existing = self.teammates.get(name)
        if existing is not None and existing.status not in (
                TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
            return existing
        if name == TEAM_VERIFIER_NAME:
            # 内置 Verifier 走专用创建通道 (不属于名册)
            return await self._ensure_verifier()
        if name not in self._roster:
            # 名册外成员不在这里兜底 (Lead 动态 spawn 新 agent 走 _on_spawn →
            # _create_teammate 路径)
            return None
        tm = await self._create_teammate(name)
        if tm is None:
            logger.warning("Lazy spawn of teammate '%s' failed", name)
            return None
        self._refresh_team_context()
        await self._event_queue.put(await self._emit_member_status(name, "idle"))
        return tm

    async def _create_teammate(self, name: str) -> TeammateAgent | None:
        """创建并 spawn 一个 TeammateAgent — 使用 ConfigLoader 加载 per-agent 配置.

        重名检查: 同名实例仍在运行 → 直接返回现有实例 (避免覆盖 dict 项导致
        旧 agent loop 泄漏且双消费同一 inbox); 同名但已 SHUTDOWN/FAILED →
        先确保旧实例 agent loop 已终结再替换.
        """
        existing = self.teammates.get(name)
        if existing is not None:
            if existing.status not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                logger.info(
                    "Teammate '%s' already running (status=%s) — reusing existing instance",
                    name, existing.status,
                )
                return existing
            # 同名但已终结 → 确保旧 agent loop 已退出再替换
            if existing._task is not None and not existing._task.done():
                try:
                    await asyncio.wait_for(existing.shutdown(), timeout=10)
                except Exception as exc:
                    logger.warning(
                        "Old teammate '%s' shutdown before respawn failed: %s", name, exc,
                    )
            self.teammates.pop(name, None)

        try:
            from harness.config.config_loader import ConfigLoader

            # 加载 member 的 EffectiveConfig (L0 + L1 + L2)
            member_eff = ConfigLoader.load_effective(
                user_id=self._user_id, agent_name=name,
            )

            # ── Phase 6: 成员级 worktree 隔离 (默认 shared — 零行为变化) ──
            # isolation=worktree 时创建独立 worktree; 非 git 仓库/创建失败
            # 降级 shared, 不阻断 spawn
            from harness.team.worktree import (
                create_member_worktree, resolve_member_isolation,
            )
            worktree_ctx = None
            if resolve_member_isolation(member_eff) == "worktree":
                worktree_ctx = await create_member_worktree(
                    project_id=self._project_id,
                    user_id=self._user_id,
                    thread_id=self._thread_id,
                    agent_name=name,
                )
                if worktree_ctx is not None:
                    self._worktree_contexts[name] = worktree_ctx

            llm = self._llm_factory(
                member_eff.model,
                temperature=member_eff.temperature,
                max_tokens=member_eff.max_tokens,
            ) if self._llm_factory else None
            if llm is None:
                logger.error("No LLM available for teammate '%s'", name)
                return None

            # 工具 — 从 member EffectiveConfig 的 tool_groups 加载
            tools: list = []
            if self._tool_registry:
                for group in member_eff.tool_groups:
                    tools.extend(self._tool_registry.get_tools_by_category(group))

            # tool_search 延迟加载: MCP 工具被 defer 时, 注入搜索工具
            # (member 的 MCP schema 由 DeferredToolFilterMiddleware 按需隐藏)
            from harness.tools.tool_search import get_tool_search_tool
            _ts_tool = get_tool_search_tool()
            if _ts_tool is not None:
                tools.append(_ts_tool)

            # 注入 team 工具 (按角色过滤)
            role = "member"  # _create_teammate 只创建 member
            lead_name = self.team_context.lead_name if self.team_context else None

            from harness.team.tools import create_team_tools
            team_tools = create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                teammates=self.teammates,
                role=role,
                spawn_callback=None,
                event_emitter=self._event_queue.put,
                lead_name=lead_name,
                progress_callback=self._progress_event.set,
                member_names=self._member_names,
                run_started_at=lambda: self._started_at,
            )
            tools.extend(team_tools)

            # Member 可委派子任务; Lead 通过 delegate_to_member + 任务板分配
            # (Lead 的 ask_clarification 在 _create_lead 中单独添加)
            from harness.tools.builtins.lead_tools import Agent_tool
            tools.append(Agent_tool(
                manager=self._subagent_manager,
            ))

            teammate = TeammateAgent(
                agent_name=name,
                llm=llm,
                tools=tools,
                team_context=self.team_context,
                message_bus=self.message_bus,
                task_store=self.task_store,
                task_memory_store=self._task_memory_store,
                member_memory_store=self._member_memory_store,
                skill_storage=self._skill_storage,
                event_queue=self._event_queue,
                role=role,
                lead_name=lead_name,
                thread_id=self._thread_id,
                project_id=self._project_id,
                tracer=self.tracer,
                effective_config=member_eff,
                checkpointer=self._checkpointer,
                llm_semaphore=self._llm_semaphore,
                worktree_virtual_path=(
                    worktree_ctx.virtual_path if worktree_ctx is not None else ""
                ),
            )
            await teammate.spawn()
            self.teammates[name] = teammate
            # 新成员加入后重新检测 Verifier (动态 spawn 的验收成员才能被发现)
            self._verifier_checked = False
            logger.info("Teammate '%s' created and spawned (role=%s)", name, role)
            return teammate

        except Exception as exc:
            logger.error("Failed to create teammate '%s': %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Lead 创建
    # ------------------------------------------------------------------

    def _build_lead_soul(self, lead_name: str) -> str:
        """生成 Lead Agent 的 SOUL — 系统预置, 不读 SOUL.md.

        关键设计:
        - 设置 Lead 的身份和行为基调 (不重复 _get_lead_instructions 中的工具使用说明)
        - _get_lead_instructions() 提供详细的操作手册 (工具、协议、通信)
        - SOUL 提供战略思维和决策原则
        """
        return f"""# Lead Agent

你是 **{lead_name}**，本团队的 Lead Agent。你是一个战略思考者:

1. **先澄清, 后行动**: 如果用户目标模糊、缺少关键信息或有多种合理方案, 使用 ask_clarification 主动提问, 不要猜测。
2. **思考 → 规划 → 委派**: 先理解用户的真实需求, 再拆解为清晰的子任务, 最后根据成员能力分配到任务板。
3. **人尽其才**: 每个 Member 有不同的工具和技能 (见 <team_capabilities>), 把任务分配给最合适的人。
4. **监控与适应**: 定期检查任务进度。如果某任务失败, 决定是重试、重新分配还是换个思路。
5. **汇总交付**: 所有任务完成后, 将结果整合为条理清晰的最终报告。

你不只是任务调度员 — 你是解决方案的架构师。团队依赖你来提供方向、判断和清晰度。"""

    async def _create_lead(self) -> TeammateAgent | None:
        """创建平台内置 Lead Agent — 使用 default 配置 + 系统 Lead SOUL.

        Lead 是平台级基础设施, 不属于项目 members 列表。
        LLM 配置始终来自全局 default agent, SOUL 由系统生成。
        Lead 的工具仅限 LEAD_ALLOWED_TOOL_GROUPS 白名单, 强制排除执行类工具。
        """
        try:
            from harness.config.config_loader import ConfigLoader

            lead_name = TEAM_LEAD_NAME
            lead_eff = ConfigLoader.load_effective(
                user_id=self._user_id, agent_name="default",
            )
            logger.info(
                "Creating platform Lead: model=%s tool_groups=%s",
                lead_eff.model, lead_eff.tool_groups,
            )

            # ── LLM ──
            llm = self._llm_factory(
                lead_eff.model,
                temperature=lead_eff.temperature,
                max_tokens=lead_eff.max_tokens,
            ) if self._llm_factory else None
            if llm is None:
                logger.error("No LLM available for Lead")
                return None

            # ── 工具: 白名单过滤, 只保留团队管理类工具 ──
            tools: list = []
            if self._tool_registry:
                for group in lead_eff.tool_groups:
                    if group in LEAD_ALLOWED_TOOL_GROUPS:
                        tools.extend(self._tool_registry.get_tools_by_category(group))
                    else:
                        logger.info(
                            "Lead: tool_group '%s' excluded by whitelist", group,
                        )

            # 注入 team 工具 (Lead 角色)
            _self = self

            async def _on_spawn(agent_name: str) -> str:
                # 同名成员仍在运行 → 不重复 spawn, 提示 Lead
                existing_tm = _self.teammates.get(agent_name)
                if (existing_tm is not None and existing_tm.status
                        not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED)):
                    return f"Teammate '{agent_name}' is already running."
                # ── 名册成员走懒加载通道 (内部负责注册/刷新快照/状态事件),
                # AgentCard 在 initialize 已生成, 无需补生成 ──
                if agent_name in _self._roster:
                    tm = await _self._ensure_teammate(agent_name)
                    if tm is not None:
                        return f"Teammate '{agent_name}' spawned successfully."
                    return f"Failed to spawn '{agent_name}': agent config not found or LLM unavailable."
                tm = await _self._create_teammate(agent_name)
                if tm:
                    _self._refresh_team_context()
                    # ── 动态 spawn 的名册外成员补生成 AgentCard → 更新 agent_card.json
                    # 与 <team_capabilities>, 让 Lead 感知其能力 (失败只 log, 不阻断);
                    # 卡片仍新鲜 (未过期) 则跳过重新生成 ──
                    try:
                        from harness.team.agent_card import (
                            generate_agent_card, load_project_cards, save_project_cards,
                            is_card_stale,
                        )
                        from harness.config.config_loader import ConfigLoader as _CL
                        if is_card_stale(
                            _self._project_id, agent_name, user_id=_self._user_id,
                        ):
                            member_eff = _CL.load_effective(
                                user_id=_self._user_id, agent_name=agent_name,
                            )
                            card = generate_agent_card(
                                agent_name,
                                user_id=_self._user_id,
                                tool_registry=_self._tool_registry,
                                skill_storage=_self._skill_storage,
                                effective_config=member_eff,
                                role="member",
                            )
                            cards = load_project_cards(
                                _self._project_id, user_id=_self._user_id,
                            )
                            cards[agent_name] = card
                            save_project_cards(
                                _self._project_id, cards, user_id=_self._user_id,
                            )
                            if _self.team_context is not None:
                                _self.team_context.set_team_capabilities(cards)
                    except Exception as exc:
                        logger.warning(
                            "Failed to generate agent card for spawned '%s': %s",
                            agent_name, exc,
                        )
                    return f"Teammate '{agent_name}' spawned successfully."
                return f"Failed to spawn '{agent_name}': agent config not found or LLM unavailable."

            from harness.team.tools import create_team_tools
            team_tools = create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                teammates=self.teammates,
                role="lead",
                spawn_callback=_on_spawn,
                event_emitter=self._event_queue.put,
                lead_name=lead_name,
                progress_callback=self._progress_event.set,
                member_names=self._member_names,
                run_started_at=lambda: self._started_at,
            )
            tools.extend(team_tools)

            # Lead 专属工具
            from harness.tools.builtins.lead_tools import ask_clarification_tool
            tools.append(ask_clarification_tool())

            # ── 创建 TeammateAgent ──
            lead_soul = self._build_lead_soul(lead_name)
            teammate = TeammateAgent(
                agent_name=lead_name,
                llm=llm,
                tools=tools,
                team_context=self.team_context,
                message_bus=self.message_bus,
                task_store=self.task_store,
                task_memory_store=self._task_memory_store,
                skill_storage=self._skill_storage,
                event_queue=self._event_queue,
                role="lead",
                lead_name=lead_name,
                thread_id=self._thread_id,
                project_id=self._project_id,
                tracer=self.tracer,
                effective_config=lead_eff,
                soul_override=lead_soul,
                checkpointer=self._checkpointer,
                llm_semaphore=self._llm_semaphore,
            )
            await teammate.spawn()
            self.teammates[lead_name] = teammate
            logger.info("Lead '%s' created (default config + system Lead SOUL)", lead_name)
            return teammate

        except Exception as exc:
            logger.error("Failed to create Lead '%s': %s", lead_name, exc)
            return None

    # ------------------------------------------------------------------
    # 内置 Verifier 创建 (平台级, 按需懒加载)
    # ------------------------------------------------------------------

    def _build_verifier_soul(self) -> str:
        """生成内置 Verifier 的 SOUL — 系统预置, 独立验收者的行为准则."""
        return f"""# Verifier

你是 **{TEAM_VERIFIER_NAME}**，团队的独立验收专家。你不参与任何实现工作,
唯一职责是验证其他成员产出的交付物。

## 核心原则

1. **独立性**: 你没有参与任务的实现过程, 不要假设实现者的任何说法为真 — 一切亲自验证。
2. **证据优先**: 验收结论必须基于你亲自检查到的证据, 不是任务报告里的声称。
3. **严格但不刁难**: 按验收标准 (acceptance_criteria) 逐条核对, 标准之外不额外发挥。

## 验收流程

1. 阅读任务的目标、描述、约束和验收标准
2. 检查执行者提交的结果和证据:
   - 文件类证据: 必须亲自读取文件, 确认存在且内容符合要求
   - 命令类证据: 可行时重新执行 (跑测试/构建), 确认真的通过
   - 无法验证的证据: 视为未通过该项
3. 对照验收标准逐条给出结论 (✅/❌ + 实际验证到的证据)

## 输出格式 (必须严格遵守)

逐条列出检查结果后, 最后一行必须是 `VERDICT: PASS` 或 `VERDICT: FAIL`。
FAIL 时必须说明哪条标准未满足、实际发现了什么。
完成后用 task_update 提交结果 (result JSON 的 output 中必须包含 VERDICT 行)。"""

    async def _create_verifier(self) -> TeammateAgent | None:
        """创建平台内置 Verifier Agent — default 配置 + 系统 Verifier SOUL.

        Verifier 是平台级基础设施 (与 Lead 同级, 不属于项目 members 列表),
        高危任务的验收子任务由它执行; 按需懒加载 (_ensure_verifier)。
        """
        try:
            from harness.config.config_loader import ConfigLoader

            eff = ConfigLoader.load_effective(
                user_id=self._user_id, agent_name="default",
            )
            llm = self._llm_factory(
                eff.model, temperature=eff.temperature, max_tokens=eff.max_tokens,
            ) if self._llm_factory else None
            if llm is None:
                logger.error("No LLM available for Verifier")
                return None

            # ── 工具: 白名单过滤 (只读+搜索, 验收不应修改交付物) ──
            tools: list = []
            if self._tool_registry:
                for group in eff.tool_groups:
                    if group in VERIFIER_ALLOWED_TOOL_GROUPS:
                        tools.extend(self._tool_registry.get_tools_by_category(group))
                    else:
                        logger.info("Verifier: tool_group '%s' excluded by whitelist", group)

            lead_name = self.team_context.lead_name if self.team_context else TEAM_LEAD_NAME
            from harness.team.tools import create_team_tools
            tools.extend(create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                teammates=self.teammates,
                role="member",
                spawn_callback=None,
                event_emitter=self._event_queue.put,
                lead_name=lead_name,
                progress_callback=self._progress_event.set,
                member_names=self._member_names,
                run_started_at=lambda: self._started_at,
            ))

            teammate = TeammateAgent(
                agent_name=TEAM_VERIFIER_NAME,
                llm=llm,
                tools=tools,
                team_context=self.team_context,
                message_bus=self.message_bus,
                task_store=self.task_store,
                task_memory_store=self._task_memory_store,
                member_memory_store=self._member_memory_store,
                skill_storage=self._skill_storage,
                event_queue=self._event_queue,
                role="member",
                lead_name=lead_name,
                thread_id=self._thread_id,
                project_id=self._project_id,
                tracer=self.tracer,
                effective_config=eff,
                soul_override=self._build_verifier_soul(),
                checkpointer=self._checkpointer,
                llm_semaphore=self._llm_semaphore,
            )
            await teammate.spawn()
            self.teammates[TEAM_VERIFIER_NAME] = teammate
            logger.info("Built-in Verifier '%s' created (default config + system SOUL)",
                        TEAM_VERIFIER_NAME)
            return teammate

        except Exception as exc:
            logger.error("Failed to create built-in Verifier: %s", exc)
            return None

    async def _ensure_verifier(self) -> TeammateAgent | None:
        """按需拉起内置 Verifier — 首个高危任务进入验收时调用."""
        existing = self.teammates.get(TEAM_VERIFIER_NAME)
        if (existing is not None and existing.status
                not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED)):
            return existing
        tm = await self._create_verifier()
        if tm is None:
            logger.warning("Lazy spawn of built-in Verifier failed")
            return None
        self._refresh_team_context()
        await self._event_queue.put(
            await self._emit_member_status(TEAM_VERIFIER_NAME, "idle"))
        return tm

    # ------------------------------------------------------------------
    # 主执行入口
    # ------------------------------------------------------------------

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """执行 Team 模式的主入口 — 异步生成器 yield SSE 事件."""
        self._started_at = _now_iso()
        self._last_progress_at = self._started_at

        # ── Tracing: 启动 Team trace ──
        self.tracer.trace_team_start(
            message=message,
            project_id=self._project_id,
            thread_id=self._thread_id,
            members=list(self.teammates.keys()),
        )

        yield {
            "type": "team_start",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            # 名册全员 (含未 spawn 的待命成员) — 懒加载下 teammates 只有 Lead
            "members": list(self._member_names),
            "mode": "team",
        }

        try:
            # ── Phase 0: Triage — Lead 判断自处理还是拆解 ──
            self.tracer.trace_phase("triage")
            yield await self._emit_team_status("triage", "Lead Agent 正在分析目标...")

            # 加载已有任务和团队记忆，让 Lead 感知任务板和团队知识积累
            existing_tasks = await self.task_store.load_tasks()
            self._stale_terminal_ids = {
                t.id for t in existing_tasks if t.status.is_terminal
            }
            triage_message = message

            team_memory_xml = self.team_context.get_team_memory_xml() if self.team_context else ""
            if team_memory_xml:
                triage_message += "\n\n" + team_memory_xml
            if existing_tasks:
                triage_message += self._format_existing_tasks(existing_tasks)

            lead = self._get_lead()
            plan_summary = ""
            if lead:
                try:
                    # 直接发送用户消息, Lead 自行判断自处理 vs 拆解 (见 <task_triage>)
                    triage_task = TeamTask(
                        id=str(uuid.uuid4())[:8],
                        project_id=self._project_id,
                        title=f"用户目标: {triage_message[:80]}",
                        description=triage_message,
                        priority="high",
                    )
                    triage_accepted = await lead.assign_task(triage_task)
                    if not triage_accepted:
                        logger.warning("Lead rejected triage task (status=%s)", lead.status)
                    # 等待 Lead 完成分析 (最多 120s), 期间持续发布进度
                    async for ev in self._wait_lead_idle(lead, timeout_s=120):
                        yield ev

                    if lead.last_error:
                        logger.warning("Lead '%s' triage error: %s", lead.name, lead.last_error)

                    # ── s32: 检测 pending clarification (Lead 调用了 ask_clarification) ──
                    if lead and lead.pending_clarification:
                        async for ev in self._emit_clarification_pause(lead, ""):
                            yield ev
                        return  # 暂停执行, 等待用户回答

                except Exception as exc:
                    logger.warning("Lead triage failed: %s, continuing", exc)

            # ── 判断: 自处理 or 拆解? ──
            all_tasks = await self.task_store.load_tasks()
            # 子任务 = 非 "用户目标:" 标题的**未完结**任务 (由 Lead 的 task_create 产生).
            # 任务板是项目级持久化的, 历史 run 的终态任务必须排除 — 否则简单
            # 问题会被误判为拆解模式, dispatch 循环又因全部终态而空转退出
            sub_tasks = [t for t in all_tasks
                         if not t.title.startswith("用户目标:") and not t.status.is_terminal]
            has_sub_tasks = len(sub_tasks) > 0

            if not has_sub_tasks:
                # Lead 分析超时仍在 WORKING → 同样走降级拆分, 不误判为自处理
                lead_timed_out = lead is not None and lead.status == TeammateStatus.WORKING
                if lead and (lead.last_error or lead_timed_out):
                    # Lead 失败/超时 → 降级为自动分配
                    logger.warning(
                        "Lead triage %s — using fallback plan",
                        "failed" if lead.last_error else "timed out",
                    )
                    yield await self._emit_team_status(
                        "triage",
                        "Lead 分析失败 (LLM 可能不可用)，自动降级为简单任务拆分。"
                        if lead.last_error else
                        "Lead 分析超时，降级为自动任务拆分。")
                    # 名册选成员 (懒加载: 未 spawn 的成员由 dispatch 按需拉起)
                    workers = [name for name in self._member_names
                              if name != (lead.name if lead else "")]
                    assigned = workers[0] if workers else next(iter(self.teammates), None)
                    if assigned:
                        await self.task_store.create_task(
                            title=message[:100],
                            description=message,
                            assigned_agent=assigned,
                            priority="high",
                        )
                        plan_summary = f"[自动降级] 将用户目标直接创建为任务，分配给 {assigned}"
                        has_sub_tasks = True
                    else:
                        plan_summary = f"[Lead 独立完成, 无可用成员]"
                else:
                    # 自处理模式: Lead 直接回答了用户
                    logger.info("Lead chose self-solve — no sub-tasks created")
                    plan_summary = "[Lead 独立完成]"
            else:
                plan_summary = f"[Lead 拆解为 {len(sub_tasks)} 个子任务]"
                logger.info("Lead created %d sub-tasks — entering dispatch", len(sub_tasks))

            # 创建用户目标根任务 (用于追踪)
            root_task = await self.task_store.create_task(
                title=f"用户目标: {message[:100]}",
                description=(
                    f"{message}\n\n"
                    + (f"【Lead 决策】\n{plan_summary}" if plan_summary else "")
                ),
                priority="high",
            )
            await self.task_store.update_task(root_task.id, status=TeamTaskStatus.COMPLETED)

            # ── Phase 2+3: Dispatch Loop + Lead Synthesis ──
            async for event in self._dispatch_and_synthesize(lead, plan_summary, has_sub_tasks):
                yield event

        except Exception as exc:
            logger.exception("Team execution failed")
            yield await self._emit_team_error(f"Team 执行失败: {exc}")

        finally:
            async for event in self._finalize_run():
                yield event

    async def resume(self, answer: str) -> AsyncIterator[dict[str, Any]]:
        """澄清回答后的恢复入口 — 异步生成器 yield SSE 事件.

        与 run() 的差异:
        - 不重复 yield team_start / 创建 triage 任务 / 创建 "用户目标:" 根任务
        - 澄清回答只投递一次 (append 进 Lead 消息缓冲), 不再作为 triage 任务文本
        - 复用 run() triage 之后的 dispatch + synthesis 逻辑, 支持再次暂停
        """
        # 刷新进度时间戳, 避免看门狗把澄清暂停时长计入死锁/整体超时
        self._started_at = _now_iso()
        self._last_progress_at = self._started_at
        self._cancelled = False

        try:
            lead = self._get_lead()
            if lead is None:
                yield await self._emit_team_error("Lead Agent 不可用，无法恢复团队运行")
                return

            question = (lead.pending_clarification or {}).get("question", "")
            logger.info("Resuming team run after clarification: %s", question[:80])
            resume_msg = (
                f"[用户澄清回答]\n"
                f"之前的问题: {question}\n"
                f"用户的回答: {answer}\n\n"
                f"请根据用户的回答继续执行。如果需要更多信息，"
                f"可以再次使用 ask_clarification。"
            )
            self._clarification_pending = False
            lead.pending_clarification = None
            # 投递回答到 Lead 消息缓冲 — 全链路唯一投递点
            lead._messages.append(HumanMessage(content=resume_msg))

            # Lead 非 IDLE 或 agent loop 已死 → respawn 重启 loop
            if lead.status != TeammateStatus.IDLE or lead._task is None or lead._task.done():
                logger.info(
                    "Lead not resumable (status=%s), respawning for resume", lead.status,
                )
                await lead.respawn()
            # 唤醒 Lead 消化回答 (与 inbox 消息路径一致: IDLE → WORKING)
            if lead.status == TeammateStatus.IDLE:
                lead.status = TeammateStatus.WORKING
                lead._wake_event.set()

            # 等待 Lead 根据回答完成重新规划 (最多 120s), 期间持续发布进度
            self.tracer.trace_phase("triage")
            yield await self._emit_team_status("triage", "Lead Agent 正在根据澄清回答继续分析...")
            async for ev in self._wait_lead_idle(lead, timeout_s=120):
                yield ev

            # ── s32: 再次暂停检测 — Lead 可能根据回答继续追问 ──
            if lead.pending_clarification:
                async for ev in self._emit_clarification_pause(lead, " again"):
                    yield ev
                return  # 暂停执行, 等待用户回答

            # ── 判断子任务 (沿用 run() 的规则: 非 "用户目标:" 标题的未完结任务) ──
            all_tasks = await self.task_store.load_tasks()
            sub_tasks = [t for t in all_tasks
                         if not t.title.startswith("用户目标:") and not t.status.is_terminal]
            has_sub_tasks = len(sub_tasks) > 0
            plan_summary = (
                f"[澄清回答后继续执行, 共 {len(sub_tasks)} 个子任务]"
                if has_sub_tasks else "[Lead 独立完成]"
            )

            # ── Phase 2+3: Dispatch Loop + Lead Synthesis (与 run() 共用) ──
            async for event in self._dispatch_and_synthesize(lead, plan_summary, has_sub_tasks):
                yield event

        except Exception as exc:
            logger.exception("Team resume failed")
            yield await self._emit_team_error(f"Team 执行失败: {exc}")

        finally:
            async for event in self._finalize_run():
                yield event

    async def _dispatch_and_synthesize(
        self, lead: TeammateAgent | None, plan_summary: str, has_sub_tasks: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        """triage 之后的共用执行段: 事件驱动 dispatch 循环 + Lead 汇总.

        run() 和 resume() 共用; 期间检测到 lead.pending_clarification 则
        yield clarification 事件并暂停 (置 _clarification_pending).
        """
        # ── Phase 2: Event-Driven Dispatch Loop (仅拆解模式) ──
        if has_sub_tasks:
            self.tracer.trace_phase("dispatching")
            yield await self._emit_team_status("dispatching", "开始事件驱动的任务调度...")

            watchdog_task = asyncio.create_task(self._watchdog())

            try:
                while not await self._is_complete() and not self._cancelled:
                    self._round += 1

                    # ── 团队成员状态保鲜 (prompt 中的 <team_members> 每轮可见最新状态) ──
                    self._refresh_team_context()

                    # ── 依赖失败传播: 级联取消下游任务 ──
                    propagated = await self.task_store.propagate_failures()
                    for ct in propagated:
                        await self._event_queue.put(await self._emit_task_update(ct))
                        lead_notify = self._get_lead()
                        if lead_notify:
                            await self.message_bus.send(TeamMessage(
                                from_agent="orchestrator", to_agent=lead_notify.name,
                                msg_type=TeamMessageType.LIFECYCLE,
                                content=(f"任务 [{ct.id}] {ct.title} "
                                         f"因依赖失败被取消: {ct.error}"),
                                task_id=ct.id,
                            ))
                    if propagated:
                        self._last_progress_at = _now_iso()

                    # ── 恢复中断任务: crash 后原成员恢复 → IN_PROGRESS ──
                    resumed = await self._resume_interrupted_tasks()
                    if resumed > 0:
                        self._last_progress_at = _now_iso()

                    # ── Phase 3: 高危任务验收 (创建验收子任务 + 消化 VERDICT) ──
                    if await self._process_verifications():
                        self._last_progress_at = _now_iso()

                    # ── 事件驱动: 等待进展, 不再 sleep() ──
                    dispatched = await self._dispatch_ready_tasks()

                    # ── 处理 event queue (SSE 输出) ──
                    while not self._event_queue.empty():
                        yield self._event_queue.get_nowait()

                    # ── s32: Lead 在 dispatch 期间也可能要求澄清 ──
                    if lead and lead.pending_clarification:
                        async for ev in self._emit_clarification_pause(lead, " (dispatch)"):
                            yield ev
                        return  # 暂停执行, 等待用户回答

                    # ── Teammate 完成通知: 让 Lead 知道进展 (去重) ──
                    for name, tm in list(self.teammates.items()):
                        if (tm.status == TeammateStatus.IDLE and tm.current_task_id is None
                                and name not in self._notified_idle):
                            self._notified_idle.add(name)
                            # ── SSE: 成员状态变更 → 前端 Members 标签 ──
                            await self._event_queue.put(await self._emit_member_status(
                                name, "idle"))
                            lead_notify = self._get_lead()
                            if lead_notify and lead_notify.name != name:
                                msg = TeamMessage(
                                    from_agent=name, to_agent=lead_notify.name,
                                    msg_type=TeamMessageType.LIFECYCLE,
                                    content=f"已完成 {tm.completed_tasks} 个任务, 等待新任务",
                                )
                                await self.message_bus.send(msg)
                                # ── SSE: 推送消息事件到前端 ──
                                await self._event_queue.put({
                                    "type": "team_message",
                                    "thread_id": self._thread_id,
                                    "project_id": self._project_id,
                                    "message": msg.model_dump(),
                                })
                        elif tm.status == TeammateStatus.WORKING:
                            # 重新进入 WORKING → 清除标记, 下次完成时可再通知
                            self._notified_idle.discard(name)

                    # ── Tracing: Lead 持续监控 (trace LIFECYCLE messages) ──
                    self.tracer.trace_message(
                        from_agent="orchestrator", to_agent=lead.name if lead else None,
                        msg_type="lifecycle",
                        content=f"Round {self._round}: dispatched={dispatched}",
                    )

                    if dispatched > 0:
                        self._last_progress_at = _now_iso()
                    else:
                        # 无任务可分配时, 等待进展事件 (最多 5s)
                        self._progress_event.clear()
                        try:
                            await asyncio.wait_for(self._progress_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass

            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # 看门狗异常不应从 finally 抛出毁掉正常 run
                    logger.exception("Watchdog raised during shutdown")

        # ── Phase 3: Lead LLM Synthesis (自处理 + 拆解模式均执行) ──
        # 自处理模式下 Lead 已在 triage 阶段直接回答了用户, 无任务产出可汇总 → 跳过
        _self_solve = plan_summary.startswith("[Lead 独立完成")
        if not _self_solve:
            self.tracer.trace_phase("synthesizing")
            yield await self._emit_team_status("synthesizing", "Lead Agent 正在汇总结果...")
            async for event in self._llm_synthesize(lead, plan_summary):
                yield event

        # ── Team Memory 提取 (fire-and-forget, 不阻塞主流程) ──
        all_tasks_final = await self.task_store.load_tasks()
        if all_tasks_final and self._team_memory_store is not None:
            asyncio.create_task(self._extract_team_memory(all_tasks_final))

    async def _finalize_run(self) -> AsyncIterator[dict[str, Any]]:
        """run()/resume() 共用的收尾逻辑 (在各自的 finally 中调用).

        暂停等待 clarification 时跳过清理, 保留 teammate 供下次 resume.
        """
        # ── s32: 暂停等待 clarification 时跳过清理 ──
        if self._clarification_pending:
            logger.info("Team run paused for clarification — keeping teammates alive")
            return

        # ── 结算残留 IN_PROGRESS (取消/超时/异常路径) — 与 recover 语义一致,
        # 避免任务板残留 IN_PROGRESS 卡死下次 run 的 _is_complete() ──
        stale_tasks = await self.task_store.list_tasks(status=TeamTaskStatus.IN_PROGRESS)
        for t in stale_tasks:
            retry_count = t.retry_count + 1
            if retry_count < t.max_retries:
                await self.task_store.update_task(
                    t.id, status=TeamTaskStatus.INTERRUPTED, retry_count=retry_count,
                    error="团队运行结束 (取消/超时/异常), 任务中断待恢复",
                )
            else:
                await self.task_store.update_task(
                    t.id, status=TeamTaskStatus.CANCELLED, retry_count=retry_count,
                    error=f"团队运行中断且已达最大重试次数 ({t.max_retries})",
                )

        # ── Tracing: 记录结束状态 ──
        final_status = "completed" if await self._is_complete() else ("cancelled" if self._cancelled else "error")
        self.tracer.trace_team_end(status=final_status, total_rounds=self._round)
        # langfuse shutdown 是同步网络 flush, 可能长时间阻塞 (曾在此造成整个
        # 进程假死: 事件循环冻结, 所有 SSE/HTTP 无响应) — 放到线程并加超时兜底
        try:
            await asyncio.wait_for(asyncio.to_thread(self.tracer.shutdown), timeout=15)
        except Exception as exc:
            logger.warning("TeamTracer shutdown skipped (timeout or error): %s", exc)

        # 清理: shutdown 所有 teammate (带超时, 防止文件 I/O 阻塞)
        _shutdown_timeout = 10  # 每个 teammate 最多等待 10 秒
        for tm in list(self.teammates.values()):
            if tm.status not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                try:
                    await asyncio.wait_for(tm.shutdown(), timeout=_shutdown_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Teammate '%s' shutdown timed out after %ds — forcing exit",
                        tm.name, _shutdown_timeout,
                    )

        # ── Phase 6: worktree 成员的改动**保留** (不 merge 不删除) ──
        # run 结束仅 log 提示路径; 回收发生在项目删除时 (按 worktrees.json 清单)
        for name, wt in self._worktree_contexts.items():
            logger.info(
                "Teammate '%s' 的 worktree 产物保留在 %s (branch=%s) — "
                "项目删除时自动回收",
                name, wt.path, wt.branch,
            )

        status = "completed" if await self._is_complete() else "cancelled" if self._cancelled else "error"
        yield {
            "type": "team_end",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "status": status,
            "total_rounds": self._round,
        }

    # ------------------------------------------------------------------
    # Interrupted task recovery
    # ------------------------------------------------------------------

    async def _resume_interrupted_tasks(self) -> int:
        """恢复 crash 后遗留的 INTERRUPTED 任务给原成员.

        每轮 dispatch 前调用, 策略:
        - 原成员 IDLE → assign_task() → IN_PROGRESS (checkpoint 自动恢复)
        - 原成员不可用 (不在 team / FAILED) → 回池 PENDING (清除 assigned_agent)
        - 原成员正忙 (WORKING) → 跳过, 留待下轮

        返回成功恢复的任务数.
        """
        interrupted_tasks = await self.task_store.list_tasks(
            status=TeamTaskStatus.INTERRUPTED,
        )
        if not interrupted_tasks:
            return 0

        resumed = 0
        for task in interrupted_tasks:
            agent_name = task.assigned_agent
            tm = self.teammates.get(agent_name) if agent_name else None

            # 原成员在名册但尚未 spawn (懒加载) → 按需拉起后走原 IDLE→恢复 逻辑
            if tm is None and agent_name in self._roster:
                tm = await self._ensure_teammate(agent_name)

            # 原成员不可用 (拉起失败/FAILED) → 回池
            if tm is None or tm.status == TeammateStatus.FAILED:
                await self.task_store.update_task(
                    task.id,
                    assigned_agent=None,
                    status=TeamTaskStatus.PENDING,
                    error=f"原成员 '{agent_name}' 不可用, 任务回池重新分配",
                )
                logger.warning(
                    "Interrupted task '%s' returned to pool (member '%s' unavailable)",
                    task.id, agent_name,
                )
                self._progress_event.set()
                await self._event_queue.put(await self._emit_task_update(task))
                continue

            # 原成员正忙 → 跳过
            if tm.status == TeammateStatus.WORKING:
                continue

            # 原成员 IDLE → 恢复
            if tm.status == TeammateStatus.IDLE:
                await self.task_store.update_task(
                    task.id, status=TeamTaskStatus.IN_PROGRESS,
                )
                accepted = await tm.assign_task(task)
                if accepted:
                    resumed += 1
                    logger.info(
                        "Interrupted task '%s' resumed by '%s' (retry %d/%d)",
                        task.id, agent_name, task.retry_count, task.max_retries,
                    )
                    self._progress_event.set()
                    await self._event_queue.put(await self._emit_task_update(task))
                    await self._event_queue.put(await self._emit_member_status(
                        agent_name, "working", task_id=task.id, task_title=task.title))
                else:
                    # 罕见: 成员拒绝 → 回池
                    await self.task_store.update_task(
                        task.id, assigned_agent=None,
                        status=TeamTaskStatus.PENDING,
                        error=f"成员 '{agent_name}' 拒绝恢复中断任务",
                    )
                    logger.warning(
                        "Interrupted task '%s': '%s' rejected resume, returned to pool",
                        task.id, agent_name,
                    )
                    self._progress_event.set()

        return resumed

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch_ready_tasks(self) -> int:
        """分配就绪任务给 IDLE teammate. 返回 dispatch 数量.

        分配策略:
        - task.assigned_agent 已指定 → 优先尊重 (Lead 的领域判断);
          成员在名册但尚未 spawn → _ensure_teammate 按需拉起 (懒加载)
        - 未指定 → 领域匹配 (全体名册+活体候选中选匹配分最高的)
        - 领域匹配失败 → 负载均衡兜底 (保证任务不卡住)

        高可用语义:
        - 指定成员已退出/拉起失败 (SHUTDOWN/FAILED/ensure 返回 None) →
          任务收回公共池, 按未指定策略重新分配;
        - 指定成员正忙 (WORKING) → 跳过, 留待下轮;
        - 受理失败 (竞态) → _assign_task_to_teammate 内部回滚任务为 PENDING.
        """
        ready_tasks = await self.task_store.get_ready_tasks()
        dispatched = 0

        for task in ready_tasks:
            # ── REVISION_NEEDED: 直接分回原成员 (不经过领域匹配) ──
            if task.status == TeamTaskStatus.REVISION_NEEDED:
                tm = self.teammates.get(task.assigned_agent) if task.assigned_agent else None
                if tm is None and task.assigned_agent in self._roster:
                    # 原成员在名册但尚未 spawn (懒加载) → 按需拉起再回派
                    tm = await self._ensure_teammate(task.assigned_agent)
                if tm is not None and tm.status == TeammateStatus.IDLE:
                    if await self._assign_task_to_teammate(tm, task):
                        dispatched += 1
                elif tm is not None and tm.status != TeammateStatus.IDLE:
                    pass  # 原成员正忙, 留待下轮
                else:
                    # 原成员不可用 (已退出/拉起失败/名册外), 回池 PENDING 重新分配
                    logger.warning(
                        "REVISION_NEEDED task '%s': original member '%s' unavailable — returning to pool",
                        task.id, task.assigned_agent,
                    )
                    await self.task_store.update_task(task.id, assigned_agent=None, status=TeamTaskStatus.PENDING)
                    self._progress_event.set()
                continue

            # ── PENDING: 原有分配逻辑 ──
            if task.status != TeamTaskStatus.PENDING:
                continue

            tm: TeammateAgent | None = None
            excluded: set[str] = set()  # 本轮拉起失败的名字 — 重选时排除, 避免死循环
            if task.assigned_agent:
                tm = self.teammates.get(task.assigned_agent)
                if tm is None and task.assigned_agent in self._roster:
                    # 指定成员在名册但尚未 spawn (懒加载) → 按需拉起
                    tm = await self._ensure_teammate(task.assigned_agent)
                    if tm is None:
                        excluded.add(task.assigned_agent)
                if tm is None or tm.status in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                    # 指定成员不可用 → 收回任务到公共池重新分配
                    logger.warning(
                        "Task '%s' assigned to unavailable teammate '%s' — returning to pool",
                        task.id, task.assigned_agent,
                    )
                    await self.task_store.update_task(task.id, assigned_agent=None)
                    task.assigned_agent = None
                    tm = None
                elif tm.status != TeammateStatus.IDLE:
                    continue  # 成员正忙, 留待下轮

            if tm is None:
                # 未指定 (或指定成员不可用已回池) → 名册+活体候选中选择,
                # 选中后按需拉起; 拉起失败则排除该名字重选, 全部失败本轮放弃
                name = self._select_best_match_teammate(task, exclude=excluded)
                while name is not None and tm is None:
                    tm = await self._ensure_teammate(name)
                    if tm is None:
                        excluded.add(name)
                        name = self._select_best_match_teammate(task, exclude=excluded)

            if tm is not None and await self._assign_task_to_teammate(tm, task):
                dispatched += 1

        return dispatched

    async def _assign_task_to_teammate(self, tm: TeammateAgent, task: TeamTask) -> bool:
        """分配任务给 teammate 并触发状态更新. 返回是否成功.

        顺序: ① 任务板原子认领(CAS) → ② 成员受理 → 失败回滚.
        认领与成员自认领共用同一个原子收口, 谁先胜出谁拿任务, 无双执行窗口;
        认领成功但成员拒绝时任务锁在我们手里, 回滚安全.
        """
        claimed = await self.task_store.claim(task.id, tm.name)
        if claimed is None:
            return False  # 已被认领/状态已变, 留待下轮
        accepted = await tm.assign_task(task)
        if not accepted:
            await self.task_store.update_task(
                task.id, assigned_agent=None, status=TeamTaskStatus.PENDING,
            )
            return False
        self._progress_event.set()
        # ── SSE: 任务更新 + 成员状态变更 (仅成功后) ──
        await self._event_queue.put(await self._emit_task_update(task))
        await self._event_queue.put(await self._emit_member_status(
            tm.name, "working", task_id=task.id, task_title=task.title))
        return True

    def _candidate_names(self, exclude: set[str] | None = None) -> list[tuple[str, int]]:
        """可派单候选 (成员名, completed_tasks) — 全体名册 + 名册外已 spawn 活体.

        懒加载语义: 名册中未 spawn 的成员视为 IDLE / completed=0 / last_error=None
        的健康候选 (选中后由 _ensure_teammate 按需拉起); 已 spawn 成员需 IDLE
        且无 last_error; Lead 始终排除.
        """
        exclude = exclude or set()
        # Lead 与内置 Verifier 不参与普通派单 (Verifier 只接验收子任务)
        exclude = exclude | {TEAM_LEAD_NAME, TEAM_VERIFIER_NAME}
        candidates: list[tuple[str, int]] = []
        seen: set[str] = set()
        for name in self._roster:
            if name in exclude:
                continue
            seen.add(name)
            tm = self.teammates.get(name)
            if tm is None:
                candidates.append((name, 0))  # 未 spawn → 待命中的健康候选
            elif tm.status == TeammateStatus.IDLE and tm.last_error is None:
                candidates.append((name, tm.completed_tasks))
        # 名册外动态 spawn 的成员同样是候选
        for name, tm in self.teammates.items():
            if name in seen or name in exclude:
                continue
            if tm.status == TeammateStatus.IDLE and tm.last_error is None:
                candidates.append((name, tm.completed_tasks))
        return candidates

    def _select_best_match_teammate(
        self, task: TeamTask, exclude: set[str] | None = None,
    ) -> str | None:
        """为未指定分配对象的任务选择最匹配的成员名 (名册 + 活体候选).

        策略 (两级):
        1. 领域匹配: 对每个候选加载 AgentCard (initialize 已为全体名册生成),
           计算与任务的匹配分, 选最高分 (需 ≥ CLAIM_THRESHOLD=15, 纯中文任务
           靠 CJK bigram 评分可达)
        2. 负载均衡兜底: 匹配分都不够 → 选已完成任务最少的候选

        返回成员名; 未 spawn 的候选由调用处 _ensure_teammate 按需拉起.
        """
        candidates = self._candidate_names(exclude)
        if not candidates:
            return None

        # ── 领域匹配 ──
        from harness.team.agent_card import get_card, compute_card_task_match

        scored: list[tuple[float, int, str]] = []
        for name, completed in candidates:
            card = get_card(self._project_id, name, user_id=self._user_id)
            if card is not None:
                score = compute_card_task_match(card, task.title, task.description)
                # 阈值 15: 加入 CJK bigram 评分后, 纯中文任务的有效匹配约 15+
                # (工具/技能命中仍是 25/30 一个), 原阈值 50 对中文任务永远达不到
                if score >= CLAIM_THRESHOLD:
                    scored.append((score, completed, name))
                    logger.debug(
                        "Domain match: task '%s' → '%s' score=%.0f", task.id, name, score,
                    )

        if scored:
            # 按匹配分降序, 同分按已完成任务数升序 (负载均衡)
            scored.sort(key=lambda x: (-x[0], x[1]))
            best = scored[0]
            logger.info(
                "Best match: task '%s' → '%s' (score=%.0f, completed=%d)",
                task.id, best[2], best[0], best[1],
            )
            return best[2]

        # ── 兜底: 负载均衡 ──
        return self._select_idle_teammate(exclude)

    def _select_idle_teammate(self, exclude: set[str] | None = None) -> str | None:
        """选择空闲且健康的成员名 (负载均衡: 已完成任务少的优先; 未 spawn 视为空闲)."""
        candidates = self._candidate_names(exclude)
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    def _refresh_team_context(self) -> None:
        """刷新 TeamContext.members — 名册 + 活体合并快照.

        懒加载语义: 未 spawn 的名册成员以 IDLE 占位出现在名单中 (未拉起
        语义上就是"可用"), 保证 Lead 的 list_teammates / prompt 始终看到
        完整团队; 已 spawn 成员用实时 to_runtime(); 名册外动态 spawn 的
        成员也并入. TeammateAgent 每个工作周期重建 system prompt, 成员
        状态/名单以这里为准.
        """
        if self.team_context is None:
            return
        members: list[TeamMemberRuntime] = []
        seen: set[str] = set()
        lead_name = self.team_context.lead_name
        ordered = ([lead_name] if lead_name else []) + [
            n for n in self._roster if n != lead_name
        ]
        for name in ordered:
            tm = self.teammates.get(name)
            if tm is not None:
                members.append(tm.to_runtime())
            else:
                # 未 spawn 的名册成员 → IDLE 占位 (懒加载: 可用但尚未拉起)
                members.append(TeamMemberRuntime(
                    agent_name=name, role="member", status=TeammateStatus.IDLE,
                ))
            seen.add(name)
        # 名册外动态 spawn 的成员 (Lead spawn_teammate 的新 agent)
        for name, tm in self.teammates.items():
            if name not in seen:
                members.append(tm.to_runtime())
        self.team_context.members = members

    def _get_lead(self) -> TeammateAgent | None:
        """获取平台内置 Lead Agent — 通过 team_context.lead_name 查找.

        注意 (语义变更): Lead 缺失时返回 None 并记录 warning, 不再退回第一个
        member — member 没有 task_review / delegate_to_member 等 Lead 工具,
        冒充 Lead 会导致 IN_REVIEW 无人审查等隐性故障. 调用方需自行处理 None
        (triage/dispatch 有 `if lead` 守卫, synthesis 有静态汇总兜底).
        """
        lead_name = self.team_context.lead_name if self.team_context else ""
        if lead_name and lead_name in self.teammates:
            return self.teammates[lead_name]
        logger.warning(
            "Lead agent '%s' not found in teammates", lead_name or TEAM_LEAD_NAME,
        )
        return None

    # ------------------------------------------------------------------
    # Phase 3: 独立 Verifier 验收
    # ------------------------------------------------------------------

    def _get_verifier(self) -> str | None:
        """返回平台内置 Verifier 的名称 — 始终可用 (懒加载由 _ensure_verifier 负责).

        Verifier 是平台内置成员 (TEAM_VERIFIER_NAME), 与 Lead 同级, 不属于项目
        members 列表; 不再按名称/SOUL 约定在项目成员中检测 (历史版本已废弃)。
        """
        return TEAM_VERIFIER_NAME

    def _build_verification_brief(self, task: TeamTask) -> str:
        """构建验收子任务的委派文本 — Verifier 独立上下文, 只拿 spec + result + evidence.

        刻意不包含实现过程/对话历史: 执行者不得验收自己的产出,
        验收者也不应被实现细节带偏 (只对照验收标准与证据真实性)。
        """
        spec_text = ""
        if task.spec is not None and not task.spec.is_empty():
            spec_text = task.spec.render()
        result = task.result
        output = task.effective_output() or "(无输出)"
        evidence_lines = "\n".join(f"- {e}" for e in result.evidence) if result and result.evidence else "(无证据)"

        return (
            f"你是**独立验收者 (Verifier)**，未参与该任务的实现，也看不到实现过程。\n"
            f"请根据以下任务规格与执行者提交的结果，逐条核对验收标准。\n\n"
            f"[任务标题]\n{task.title}\n\n"
            + (f"[任务规格]\n{spec_text}\n\n" if spec_text else f"[任务描述]\n{task.description}\n\n")
            + f"[执行者提交的结果]\n{output}\n\n"
            f"[执行者提交的证据]\n{evidence_lines}\n\n"
            f"[验收要求]\n"
            f"1. 逐条核对验收标准是否满足 (无验收标准时核对目标是否达成)\n"
            f"2. 必须验证证据真实性: 文件路径要实际读取确认内容存在且符合描述, "
            f"命令类证据可复跑验证\n"
            f"3. 不要修改实现本身, 你只做验收\n"
            f"4. 完成后用 task_update 提交: "
            f"task_update(task_id=\"<本任务id>\", status=\"completed\", "
            f"result={{\"output\": \"...\"}}), output 中必须包含单独一行:\n"
            f"   VERDICT: PASS  — 验收通过, 后附理由\n"
            f"   VERDICT: FAIL  — 验收不通过, 后附具体不合格项 (将打回执行者修改)"
        )

    async def _process_verifications(self) -> bool:
        """高危任务验收处理 (每轮 dispatch 调用). 返回是否有进展.

        两步:
        1. 为 IN_REVIEW 的高危任务创建验收子任务 (有 Verifier 且非执行者自验时);
           无 Verifier → 不处理, 留 Lead task_review 兜底
        2. 消化验收子任务的 VERDICT:
           PASS → 原任务 APPROVED; FAIL → REVISION_NEEDED (附理由打回执行者);
           解析失败/Verifier 崩溃 → 原任务留 IN_REVIEW 转 Lead 审查 (fail-safe)
        """
        processed_key = "_verdict_processed"
        verdict_processed: set[str] = getattr(self, processed_key, None) or set()
        setattr(self, processed_key, verdict_processed)

        tasks = await self.task_store.load_tasks()
        by_id = {t.id: t for t in tasks}
        progress = False

        # ── 1. 创建验收子任务 ──
        verifier = self._get_verifier()
        if verifier is not None:
            # 先确认内置 Verifier 能拉起 (首个高危任务时懒加载); 拉起失败则
            # 不建子任务, IN_REVIEW 任务留待看门狗 nudge Lead 审查兜底
            if await self._ensure_verifier() is None:
                verifier = None
        if verifier is not None:
            verified_ids = {v.verifies_task_id for v in tasks if v.verifies_task_id}
            for t in tasks:
                if t.status != TeamTaskStatus.IN_REVIEW:
                    continue
                if (t.risk or "low") != "high":
                    continue
                if t.verifies_task_id:
                    continue  # 验收子任务自身不再被验收 (防无限递归)
                if t.id in verified_ids:
                    continue  # 已有验收子任务
                if verifier == t.assigned_agent:
                    # 执行者不得验收自己的产出 → 回退 Lead 审查
                    logger.warning(
                        "Task '%s': verifier '%s' is the executor — fallback to lead review",
                        t.id, verifier,
                    )
                    continue
                vtask = await self.task_store.create_task(
                    title=f"验收: {t.title}",
                    description=self._build_verification_brief(t),
                    assigned_agent=verifier,
                    priority=t.priority,
                    verifies_task_id=t.id,
                    risk="low",  # 验收任务本身低危 (避免验收链递归)
                )
                logger.info(
                    "Verification task '%s' created for high-risk task '%s' → verifier '%s'",
                    vtask.id, t.id, verifier,
                )
                await self._event_queue.put(await self._emit_task_update(vtask))
                self._progress_event.set()
                progress = True

        # ── 2. 消化 VERDICT ──
        tasks = await self.task_store.load_tasks()  # 重新加载 (含新建子任务)
        by_id = {t.id: t for t in tasks}
        for v in tasks:
            if not v.verifies_task_id or v.id in verdict_processed:
                continue
            orig = by_id.get(v.verifies_task_id)
            if orig is None or orig.status != TeamTaskStatus.IN_REVIEW:
                # 原任务已被 Lead/其他路径处理 → 不再消化该验收结论
                verdict_processed.add(v.id)
                continue
            if v.status not in (
                TeamTaskStatus.IN_REVIEW, TeamTaskStatus.COMPLETED,
                TeamTaskStatus.APPROVED, TeamTaskStatus.FAILED,
                TeamTaskStatus.CANCELLED,
            ):
                continue  # 验收仍在执行中, 留待下轮
            verdict_processed.add(v.id)

            if v.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
                # Verifier 崩溃/被取消 → fail-safe: 原任务留 IN_REVIEW 转 Lead 审查
                logger.warning(
                    "Verification task '%s' ended as %s — task '%s' falls back to lead review",
                    v.id, v.status.value, orig.id,
                )
                continue

            verdict, reason = _parse_verdict(v.effective_output())

            # 验收任务收口: 还停留在 IN_REVIEW (Verifier 误走了审查流) → 直置 COMPLETED
            if v.status == TeamTaskStatus.IN_REVIEW:
                await self.task_store.update_task(v.id, status=TeamTaskStatus.COMPLETED)

            if verdict == "pass":
                feedback = f"Verifier 验收通过: {reason or '符合验收标准'}"
                updated = await self.task_store.update_task(
                    orig.id, status=TeamTaskStatus.APPROVED, review_feedback=feedback,
                )
                logger.info("Task '%s' APPROVED by verifier: %s", orig.id, reason[:120])
                if updated:
                    await self._event_queue.put(await self._emit_task_update(updated))
                await self._notify_executor(orig, f"✅ 验收通过 — {reason or '符合验收标准'}")
                progress = True
            elif verdict == "fail":
                new_revision = orig.revision_count + 1
                feedback = f"Verifier 验收不通过: {reason or '未说明理由'}"
                updated = await self.task_store.update_task(
                    orig.id,
                    status=TeamTaskStatus.REVISION_NEEDED,
                    review_feedback=feedback,
                    revision_count=new_revision,
                )
                logger.info("Task '%s' REJECTED by verifier: %s", orig.id, reason[:120])
                if updated:
                    await self._event_queue.put(await self._emit_task_update(updated))
                await self._notify_executor(
                    orig,
                    f"↩️ 验收不通过 (第 {new_revision} 次修改), 请根据验收意见修改后重新提交:\n{reason}",
                )
                progress = True
            else:
                # VERDICT 解析失败 → fail-safe: 原任务留 IN_REVIEW 转 Lead 审查
                logger.warning(
                    "Verification task '%s' output has no parseable VERDICT — "
                    "task '%s' falls back to lead review",
                    v.id, orig.id,
                )

        return progress

    async def _notify_executor(self, task: TeamTask, content: str) -> None:
        """验收结论通知原执行者 (best-effort)."""
        if not task.assigned_agent:
            return
        try:
            msg = TeamMessage(
                from_agent="verifier", to_agent=task.assigned_agent,
                msg_type=TeamMessageType.TEXT,
                content=f"任务 [{task.id}] '{task.title}' {content}",
                task_id=task.id,
            )
            await self.message_bus.send(msg)
        except Exception as exc:
            logger.debug("Failed to notify executor of task '%s': %s", task.id, exc)

    # ------------------------------------------------------------------
    # 完成检测
    # ------------------------------------------------------------------

    async def _is_complete(self) -> bool:
        """所有任务是否都到达终态."""
        for tm in self.teammates.values():
            if tm.status == TeammateStatus.WORKING:
                return False
        tasks = await self.task_store.load_tasks()
        for t in tasks:
            if not t.status.is_terminal:
                return False
        return True

    async def _wait_lead_idle(
        self, lead: TeammateAgent, timeout_s: float, *, emit_error: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """轮询等待 Lead 完成当前工作 (回到 IDLE 或失败), 期间 drain event queue.

        emit_error=True 时 Lead 失败会额外 yield 一条 team_error 事件 (triage 阶段);
        synthesis 阶段传 False, 失败由调用方走静态汇总兜底.
        """
        for _ in range(int(timeout_s * 2)):  # 0.5s 一步
            if lead.status == TeammateStatus.IDLE:
                break
            if lead.status == TeammateStatus.FAILED or lead.last_error:
                if emit_error:
                    yield await self._emit_team_error(
                        f"Lead Agent 分析失败: {lead.last_error or '未知错误'}")
                break
            # drain event queue
            while not self._event_queue.empty():
                yield self._event_queue.get_nowait()
            await asyncio.sleep(0.5)
        # drain 循环结束后剩余的事件
        while not self._event_queue.empty():
            yield self._event_queue.get_nowait()

    async def _emit_clarification_pause(
        self, lead: TeammateAgent, stage: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Lead 请求澄清 → yield clarification 事件并置暂停标记.

        调用方在 ``async for`` 之后必须 return (暂停执行, 等待用户回答).
        stage 仅用于 log 文案区分暂停发生的阶段 (如 " (dispatch)").
        """
        yield {
            "type": "clarification",
            "request": lead.pending_clarification,
            "thread_id": self._thread_id,
        }
        self._clarification_pending = True
        logger.info(
            "Team run paused for clarification%s: %s",
            stage, lead.pending_clarification.get("question", "")[:80],
        )

    async def _llm_synthesize(
        self, lead: TeammateAgent | None, plan_summary: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """让 Lead LLM 智能汇总所有任务结果 (替代静态 dump)."""
        all_tasks = await self.task_store.load_tasks()
        # 只汇总本 run 的产出 — 排除 run 开始前已终态的历史任务,
        # 以及 Phase 3 验收子任务 (其结论已通过原任务的 review_feedback 体现)
        completed = [t for t in all_tasks if t.status.is_success
                     and t.id not in self._stale_terminal_ids
                     and t.verifies_task_id is None
                     and not t.title.startswith("规划:") and not t.title.startswith("用户目标:")]
        # 失败汇总含级联取消的任务 (error 中带依赖失败原因)
        failed = [t for t in all_tasks
                  if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)
                  and t.id not in self._stale_terminal_ids
                  and t.verifies_task_id is None]

        if not completed and not failed:
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": "Team 执行完成, 但没有任何任务产出结果。", "msg_type": "text"}
            return

        if lead is None or lead.status == TeammateStatus.FAILED:
            # fallback: 静态汇总
            async for event in self._synthesize_results():
                yield event
            return

        # ── 构建汇总任务给 Lead ──
        # 产出读取双路径兼容: 有 result 用 result.output, 无则回退旧 output 字段
        completed_summaries = "\n".join(
            f"- [{t.id}] {t.title} (执行者: {t.assigned_agent or '未知'})\n"
            f"  输出: {t.effective_output()[:300] if t.effective_output() else '(无输出)'}"
            # Phase 3: 验收结论 (Verifier/Lead 审查反馈) 一并纳入汇总
            + (f"\n  验收: {t.review_feedback[:200]}" if t.review_feedback else "")
            for t in completed
        )
        failed_summaries = "\n".join(
            f"- [{t.id}] {t.title} (执行者: {t.assigned_agent or '未知'}): "
            f"{t.effective_failure_reason() or '未知错误'}"
            for t in failed
        ) if failed else "无"

        synthesis_task = TeamTask(
            id=str(uuid.uuid4())[:8],
            project_id=self._project_id,
            title=f"汇总: 团队执行结果",
            description=(
                f"你是一个 Team 的 Lead Agent。请汇总以下团队执行结果, 生成最终报告，用以回答用户问题\n\n"
                f"【原始目标】\n{plan_summary or '见上文'}\n\n"
                f"【已完成任务 ({len(completed)} 个)】\n{completed_summaries}\n\n"
                f"【失败任务 ({len(failed)} 个)】\n{failed_summaries}\n\n"
                f"请生成汇总报告, 包括:\n"
                f"1. 目标达成情况\n"
                f"2. 各任务的结果摘要\n"
                f"3. 失败任务的原因和建议\n"
                f"4. 后续建议 (如有)"
            ),
            priority="high",
        )
        # 清掉陈旧错误, 下面的 break 条件只认本轮新错误
        lead.last_error = None
        accepted = await lead.assign_task(synthesis_task)
        if not accepted:
            # Lead 不可受理 (竞态/异常状态) → 静态汇总兜底, 保证用户一定拿得到结果
            logger.warning("Lead rejected synthesis task (status=%s) — 静态汇总兜底", lead.status)
            async for event in self._synthesize_results():
                yield event
            return

        # 等待 Lead 完成汇总 (最多 30s)
        async for ev in self._wait_lead_idle(lead, timeout_s=30, emit_error=False):
            yield ev

        # ── s32: Lead 在汇总阶段也可能要求澄清 → 暂停等待用户回答 ──
        if lead.pending_clarification:
            async for ev in self._emit_clarification_pause(lead, " (synthesis)"):
                yield ev
            return

        # ── 汇总超时仍在 WORKING → 警告 + 静态汇总兜底, 不静默 return ──
        if lead.status == TeammateStatus.WORKING:
            logger.warning("Lead synthesis timed out — 静态汇总兜底")
            yield await self._emit_team_status(
                "synthesizing", "Lead 汇总超时，改用静态汇总输出结果。")
            async for event in self._synthesize_results():
                yield event

    async def _synthesize_results(self) -> AsyncIterator[dict[str, Any]]:
        """静态汇总 (Lead 不可用/拒绝/超时时的 fallback, 由 _llm_synthesize 调用)."""
        all_tasks = await self.task_store.load_tasks()
        # 只汇总本 run 的产出 — 排除 run 开始前已终态的历史任务,
        # "规划:"/"用户目标:" 等编排用的元任务, 以及 Phase 3 验收子任务
        # (验收结论已通过原任务的 review_feedback 体现)
        completed = [t for t in all_tasks if t.status.is_success
                     and t.id not in self._stale_terminal_ids
                     and t.verifies_task_id is None
                     and not t.title.startswith("规划:") and not t.title.startswith("用户目标:")]
        failed = [t for t in all_tasks
                  if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)
                  and t.id not in self._stale_terminal_ids
                  and t.verifies_task_id is None]

        if completed:
            # 产出读取双路径兼容: 优先 result.output, 回退旧 output 字段
            parts = []
            for t in completed:
                body = t.effective_output() or '(无输出)'
                # 有结构化证据时一并展示
                if t.result is not None and t.result.evidence:
                    body += "\n\n**证据**: " + "; ".join(t.result.evidence)
                # Phase 3: 验收结论 (Verifier/Lead 审查反馈) 一并展示
                if t.review_feedback:
                    body += f"\n\n**验收**: {t.review_feedback[:300]}"
                parts.append(
                    f"## {t.title}\n**执行者**: {t.assigned_agent or '未知'}\n\n{body}"
                )
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": f"# Team 执行结果\n\n" + "\n\n---\n\n".join(parts),
                   "msg_type": "text"}

        if failed:
            failed_list = "\n".join(
                f"- {t.title} ({t.assigned_agent or '未知'}): "
                f"{t.effective_failure_reason() or '未知错误'}"
                for t in failed)
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": f"# 失败任务\n\n{failed_list}", "msg_type": "error"}

        if not completed and not failed:
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": "Team 执行完成, 但没有任何任务产出结果。", "msg_type": "text"}

    # ------------------------------------------------------------------
    # cancel / watchdog / SSE helpers (与旧版保持兼容)
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        """取消所有 teammate 并终止澄清暂停状态.

        澄清暂停时全员 IDLE, 只杀 WORKING 会一个都不杀 — 因此 shutdown
        所有非 SHUTDOWN/FAILED 的 teammate, 保证 cancel 后 run 能彻底结束.
        """
        self._cancelled = True
        self._clarification_pending = False
        for tm in list(self.teammates.values()):
            if tm.status not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                await tm.shutdown()

    async def _reap_crashed_teammates(self) -> None:
        """回收崩溃成员: 状态 WORKING 但 agent loop 已终止.

        将其标记为 FAILED 并从总线注销; 其手上的 IN_PROGRESS 任务回收为未分配
        PENDING (retry_count+1), 交给其他成员有界重试; 超过 max_retries 则置 FAILED.
        """
        for tm in list(self.teammates.values()):
            if (tm.status == TeammateStatus.WORKING and tm._task is not None
                    and tm._task.done()):
                logger.error(
                    "Watchdog: teammate '%s' agent loop terminated unexpectedly", tm.name)
                tm.status = TeammateStatus.FAILED
                tm.last_error = tm.last_error or "agent loop terminated unexpectedly"
                self.message_bus.unregister_agent(tm.name)
                crashed_task_id = tm.current_task_id
                tm.current_task_id = None
                if crashed_task_id:
                    crashed = await self.task_store.get_task(crashed_task_id)
                    if crashed is not None and crashed.status == TeamTaskStatus.IN_PROGRESS:
                        if crashed.retry_count < crashed.max_retries:
                            # 回收任务到公共池, 让其他成员接手 (有界重试).
                            # 附上前次失败原因 (Prior attempts), 下一个接手的成员不再盲试
                            note = (f"\n\n[前次执行失败 (第 {crashed.retry_count + 1} 次尝试): "
                                    f"成员 '{tm.name}' 异常退出 — {tm.last_error or '未知原因'}]")
                            await self.task_store.update_task(
                                crashed_task_id, assigned_agent=None,
                                status=TeamTaskStatus.PENDING,
                                retry_count=crashed.retry_count + 1,
                                description=(crashed.description or "") + note)
                            self._progress_event.set()
                            logger.warning(
                                "Watchdog: task '%s' requeued (retry %d/%d)",
                                crashed_task_id, crashed.retry_count + 1, crashed.max_retries)
                        else:
                            await self.task_store.update_task(
                                crashed_task_id, status=TeamTaskStatus.FAILED,
                                error=f"执行成员 '{tm.name}' 崩溃且已达最大重试次数")
                await self._event_queue.put(
                    await self._emit_member_status(tm.name, "failed"))

    async def _watchdog(self) -> None:
        """后台看门狗: 整体超时、死锁检测、崩溃成员回收."""
        while True:
            await asyncio.sleep(5)
            if self._cancelled:
                return

            # 整体超时
            if self._started_at:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(self._started_at)).total_seconds()
                if elapsed > OVERALL_TIMEOUT:
                    logger.warning("Watchdog: overall timeout (%.0fs)", elapsed)
                    await self._event_queue.put(
                        await self._emit_team_error(f"Team 执行超过超时限制 ({OVERALL_TIMEOUT}s)"))
                    self._cancelled = True
                    return

            # ── 崩溃成员回收: 状态 WORKING 但 agent loop 已终止 → 任务有界重试 ──
            await self._reap_crashed_teammates()

            # 死锁检测
            if self._last_progress_at:
                since = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(self._last_progress_at)).total_seconds()
                if since > DEADLOCK_TIMEOUT:
                    ready = await self.task_store.get_ready_tasks()
                    # ── 统计排除 Lead: Lead 不参与派单 (_select_best_match_teammate 排除),
                    # 计入 idle 会在"所有 member 阵亡、仍有就绪任务"时漏判死锁 ──
                    busy = sum(1 for name, tm in self.teammates.items()
                               if name != TEAM_LEAD_NAME
                               and tm.status == TeammateStatus.WORKING)
                    idle = sum(1 for name, tm in self.teammates.items()
                               if name != TEAM_LEAD_NAME
                               and tm.status == TeammateStatus.IDLE)
                    # 无进展且无推进可能: 无人在干活, 且 (无就绪任务 或 有就绪任务但无人能接)
                    if busy == 0 and (not ready or idle == 0):
                        # ── 存在 IN_REVIEW 任务且 Lead 存活 → 等 Lead 审查是健康状态,
                        # 提醒 Lead task_review 并刷新进度时间戳, 不判死锁 ──
                        in_review = await self.task_store.list_tasks(
                            status=TeamTaskStatus.IN_REVIEW,
                        )
                        # ── Phase 3: 有在途验收子任务的高危任务 — 等 Verifier 是健康状态,
                        # 不计入待 Lead 审查 (验收子任务自身走正常派单, 看门狗按 busy/ready 判) ──
                        all_tasks_wd = await self.task_store.load_tasks()
                        verifying_ids = {
                            v.verifies_task_id for v in all_tasks_wd
                            if v.verifies_task_id and not v.status.is_terminal
                        }
                        in_review = [t for t in in_review if t.id not in verifying_ids]
                        lead = self._get_lead()
                        if in_review and lead is not None and lead.status not in (
                                TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                            logger.info(
                                "Watchdog: %d task(s) awaiting lead review — nudging lead",
                                len(in_review),
                            )
                            await self.message_bus.send(TeamMessage(
                                from_agent="orchestrator", to_agent=lead.name,
                                msg_type=TeamMessageType.LIFECYCLE,
                                content=(
                                    f"有 {len(in_review)} 个任务处于 in_review 状态等待你审查, "
                                    f"请使用 task_review 处理 (approved / revision_needed)"
                                ),
                                task_id=in_review[0].id,
                            ))
                            self._last_progress_at = _now_iso()
                            continue
                        logger.warning("Watchdog: deadlock detected (ready=%d, idle=%d)",
                                       len(ready), idle)
                        await self._event_queue.put(
                            await self._emit_team_error(f"死锁检测 ({DEADLOCK_TIMEOUT}s 无进展)"))
                        self._cancelled = True
                        return

            # 循环依赖检测
            cycles = await self.task_store.check_circular_dependency()
            if cycles:
                cycle_strs = [" → ".join(c) for c in cycles]
                await self._event_queue.put(
                    await self._emit_team_error(f"检测到任务依赖环: {'; '.join(cycle_strs)}"))
                self._cancelled = True
                return

    @staticmethod
    def _format_existing_tasks(tasks: list[TeamTask]) -> str:
        """将已有任务格式化为 Lead triage 阶段的上下文注入.

        历史终态任务 (此前 run 已完成/失败/取消) 折叠为一行计数 — 任务板是
        thread 级持久化的, 全量列出会让 Lead 把历史任务计入本次目标并反复
        汇报累计进度 (E2E 观察到的行为)。
        """
        icons = {
            "pending": "⏳", "in_progress": "🔄", "in_review": "👁️",
            "revision_needed": "↩️", "approved": "✅", "completed": "✅",
            "failed": "❌", "cancelled": "🚫",
        }
        active = [t for t in tasks if not t.status.is_terminal]
        terminal_count = len(tasks) - len(active)

        parts = [f"\n\n<existing_tasks>\n任务板上有 {len(active)} 个未完结任务"]
        if terminal_count:
            parts.append(
                f"，另有 {terminal_count} 个历史终态任务 (此前运行已结束, "
                f"**不属于本次目标, 不要计入进度汇报**)")
        parts.append(":")
        for t in active:
            icon = icons.get(t.status.value, "❓")
            agent = t.assigned_agent or "未分配"
            parts.append(f"- {icon} [{t.id}] {t.title} → {agent} ({t.status.value})")
        parts.append(
            "\n你可以:\n"
            "- task_list() 查看任务详情\n"
            "- task_update(task_id, status=\"cancelled\") 取消不需要的任务\n"
            "- 直接基于已有任务继续工作 (创建新任务、分配未完成的任务等)\n"
            "- task_review(task_id, ...) 审查已提交 in_review 的任务\n"
            "</existing_tasks>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Team Memory 提取
    # ------------------------------------------------------------------

    async def _distribute_member_lessons(self, all_tasks: list[TeamTask]) -> None:
        """run 结束结算: 终态任务的成员领域经验按 assigned_agent 路由写 L3.

        程序式提取 (extract_lessons_from_task, 不跑 LLM), 与 teammate 完成时
        的写入共用 source_task_id 幂等去重 — 历史任务不重复写。
        晋升检查在 add_lesson 内自动触发。失败静默: best-effort。
        """
        try:
            if self._member_memory_store is None:
                return
            for t in all_tasks:
                if not t.status.is_terminal or not t.assigned_agent:
                    continue
                # Phase 3: 验收子任务不产生成员经验 (结论是原任务的验收信号)
                if t.verifies_task_id is not None:
                    continue
                for kind, text in extract_lessons_from_task(t):
                    await self._member_memory_store.add_lesson(
                        self._project_id, t.assigned_agent, kind, text,
                        source_task_id=t.id,
                    )
        except Exception as exc:
            logger.warning("Member lesson distribution failed: %s", exc)

    async def _extract_team_memory(self, all_tasks: list[TeamTask]) -> None:
        """Extract team-level insights after a run completes (fire-and-forget).

        Phase 4 记忆分层改造:
        - L3 成员领域经验: 程序式提取, 按 assigned_agent 路由写各成员 L3
          (先执行, 独立 try — 不依赖 LLM, 也不被后续 LLM 失败影响)
        - L2 团队协作教训: 轻量 LLM 只提取协作层面教训 (谁擅长什么/配合踩坑),
          成员个人领域经验不写 L2 — 那是 L3/L1 的职责
        """
        await self._distribute_member_lessons(all_tasks)
        try:
            from harness.memory.prompt import TEAM_MEMORY_UPDATE_PROMPT
            from harness.memory.updater import _create_memory_model, _extract_text

            # ── build tasks summary ──
            # Phase 3: 排除验收子任务 (结论已通过原任务的 review_feedback 体现)
            completed = [t for t in all_tasks if t.status.is_success
                         and t.verifies_task_id is None
                         and not t.title.startswith("规划:") and not t.title.startswith("用户目标:")]
            failed = [t for t in all_tasks if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)
                      and t.verifies_task_id is None]

            if not completed and not failed:
                return

            # 产出读取双路径兼容: 有 result 用 result 字段, 无则回退旧 output/error
            def _task_line(t: TeamTask) -> str:
                line = f"- [{t.id}] {t.title} → {t.assigned_agent or '未知'} ({t.status.value})"
                if t.status.is_success:
                    out = t.effective_output()
                    if out:
                        line += f": {out[:200]}"
                    # Phase 3: 验收结论 (Verifier/Lead 审查反馈) 一并纳入记忆原料
                    if t.review_feedback:
                        line += f" [验收: {t.review_feedback[:120]}]"
                else:
                    reason = t.effective_failure_reason()
                    if reason:
                        line += f": {reason[:200]}"
                return line

            tasks_text = "\n".join(_task_line(t) for t in (completed + failed))

            # ── lead summary: 根任务 ("用户目标:") 本身无 output, 直接用统计兜底 ──
            lead_summary = f"完成 {len(completed)} 个任务, 失败 {len(failed)} 个任务"

            # ── current team memory ──
            import json as _json
            current_memory = _json.dumps(
                (await self._team_memory_store.load()).to_dict(),
                ensure_ascii=False, indent=2,
            )

            # ── build prompt and call LLM ──
            prompt = TEAM_MEMORY_UPDATE_PROMPT.format(
                current_memory=current_memory,
                tasks_summary=tasks_text,
                lead_summary=lead_summary,
            )

            # ── lightweight LLM ──
            eff = self._effective_config
            api_key = eff.api_key if eff else ""
            base_url = eff.base_url if eff else ""
            model_name = (
                eff.memory_model
                or eff.model
                or "gpt-4o-mini"
            ) if eff else "gpt-4o-mini"
            import os as _os
            api_key = api_key or _os.environ.get("OPENAI_API_KEY", "")
            base_url = base_url or _os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

            model = _create_memory_model(model_name, api_key=api_key, base_url=base_url)
            if model is None:
                return

            response = await model.ainvoke(prompt)
            text = _extract_text(response.content).strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            updates = _json.loads(text)

            # ── merge ──
            result = await self._team_memory_store.merge_updates(
                new_practices=updates.get("new_practices"),
                new_pitfalls=updates.get("new_pitfalls"),
                run_summary={
                    "thread_id": self._thread_id,
                    "summary": (updates.get("run_summary") or {}).get("summary", lead_summary[:200]),
                    "tasks_completed": len(completed),
                    "tasks_failed": len(failed),
                },
            )
            logger.info(
                "Team memory updated: %d practices, %d pitfalls, %d runs",
                len(result.best_practices), len(result.known_pitfalls),
                len(result.recent_runs),
            )
        except _json.JSONDecodeError:
            logger.warning("Failed to parse team memory LLM response")
        except Exception as exc:
            logger.warning("Team memory extraction failed: %s", exc)

    async def _emit_team_status(self, phase: str, message: str) -> dict[str, Any]:
        return {"type": "team_status", "thread_id": self._thread_id,
                "project_id": self._project_id, "phase": phase, "content": message}

    async def _emit_team_error(self, message: str) -> dict[str, Any]:
        return {"type": "team_error", "thread_id": self._thread_id,
                "project_id": self._project_id, "content": message}

    async def _emit_task_update(self, task: TeamTask) -> dict[str, Any]:
        """发射任务更新 SSE 事件 → 前端 team-store.addTask()."""
        return {
            "type": "team_task_update",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "task": task.model_dump(),
        }

    async def _emit_member_status(
        self, agent_name: str, status: str, task_id: str = "", task_title: str = "",
    ) -> dict[str, Any]:
        """发射成员运行时状态 SSE 事件 → 前端 team-store.updateMemberStatus()."""
        return {
            "type": "member_status",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "agent_name": agent_name,
            "status": status,
            "task_id": task_id or "",
            "current_task_id": task_id or "",
            "task_title": task_title or "",
            "started_at": _now_iso() if status == "working" else "",
        }
