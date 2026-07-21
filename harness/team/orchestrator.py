"""TeamOrchestrator — 事件驱动的多 Agent 编排器.

核心流程 (参考 learn-claude-code  Lead +  autonomous):
    PLANNING → SPAWN → EVENT_LOOP → SYNTHESIS → COMPLETED

与旧版关键差异:
- 用 TeammateAgent (持久化, 有自己 agent loop)
- 事件驱动唤醒替代 sleep() 忙等轮询
- Lead 在 event loop 中持续参与, 可动态 spawn/liquidate teammate
- PHASE 2 不再是 blind dispatch, 而是 Lead agent 持续 ReAct 决策
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

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
from harness.observability.team_tracer import TeamTracer
from harness.team.task_store import TeamTaskStore
from harness.team.teammate_agent import TeammateAgent
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── 常量 ──
OVERALL_TIMEOUT = 1800         # Team 整体超时 30 分钟
DEADLOCK_TIMEOUT = 120         # 死锁 2 分钟无进展

# 平台内置 Lead Agent 的保留名称 (双下划线前缀防止与用户 agent 冲突)
TEAM_LEAD_NAME = "__team_lead__"

# Lead Agent 允许加载的 tool_groups 白名单 — 仅保留搜索/只读文件/MCP,
# 排除 files(含写入)、sandbox、code 等执行类工具, 确保 Lead 只做意图识别+任务分发
LEAD_ALLOWED_TOOL_GROUPS = {"search", "files_readonly", "mcp"}


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
        self.task_store = TeamTaskStore(project_id, user_id)
        self.message_bus = TeamMessageBus(project_id, user_id)
        self.team_context: TeamContext | None = None
        self.teammates: dict[str, TeammateAgent] = {}  # agent_name → TeammateAgent

        # ── 调度状态 ──
        self._round: int = 0
        self._cancelled: bool = False
        self._started_at: str = ""
        self._last_progress_at: str = ""
        self._progress_event = asyncio.Event()

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

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """加载项目、生成 agent cards、创建 TeammateAgent 池.

        流程:
        1. 加载项目 JSON → 获取成员列表 (纯执行者)
        2. 为所有成员生成 agent-card.json — 注入 TeamContext
        3. 创建平台内置 Lead Agent (TEAM_LEAD_NAME, default 配置 + 系统 SOUL)
        4. 创建 Member Agent (各自的 L0+L1+L2 配置)
        """
        project = await _load_project_json(self._project_id, self._user_id)
        if project is None:
            raise ValueError(f"Project '{self._project_id}' not found")

        project_name = project.get("name", self._project_id)
        project_description = project.get("description", "")
        member_names: list[str] = project.get("members", [])

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
            thread_id=self._thread_id,
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
        failed_members: list[str] = []
        lead = await self._create_lead()
        if lead is None:
            failed_members.append(lead_name)

        # ── 4. 创建 Member Agent (所有项目成员) ──
        for name in member_names:
            tm = await self._create_teammate(name)
            if tm is None:
                failed_members.append(name)

        # ── 检查 ──
        all_expected = [lead_name] + member_names
        if not self.teammates:
            raise ValueError(
                f"Team 初始化失败: 所有成员 ({', '.join(member_names)}) 都无法创建。"
                f" 请检查每个 Agent 的配置 (model/api_key) 是否正确。"
            )

        if failed_members:
            logger.warning(
                "TeamOrchestrator: %d/%d members failed to create: %s",
                len(failed_members), len(all_expected), ", ".join(failed_members),
            )
            await self._event_queue.put({
                "type": "team_status",
                "thread_id": self._thread_id,
                "project_id": self._project_id,
                "phase": "init",
                "content": (
                    f"警告: {len(failed_members)} 个成员创建失败 ({', '.join(failed_members)})。"
                    f" 请检查这些 Agent 是否存在且配置了有效的 model。"
                ),
            })

        logger.info(
            "TeamOrchestrator initialized: project=%s teammates=%d/%d (1 lead + %d members) lead=%s",
            self._project_id, len(self.teammates), len(all_expected), len(member_names), lead_name,
        )

        # ── 5. 团队成员快照保鲜 (spawn 完成后状态已从 SPAWNING 变为 IDLE) ──
        self._refresh_team_context()

    async def _create_teammate(self, name: str) -> TeammateAgent | None:
        """创建并 spawn 一个 TeammateAgent — 使用 ConfigLoader 加载 per-agent 配置."""
        try:
            from harness.config.config_loader import ConfigLoader

            # 加载 member 的 EffectiveConfig (L0 + L1 + L2)
            member_eff = ConfigLoader.load_effective(
                user_id=self._user_id, agent_name=name,
            )

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

            # 注入 team 工具 (按角色过滤)
            role = "member"  # _create_teammate 只创建 member
            lead_name = self.team_context.lead_name if self.team_context else None

            from harness.team.tools import create_team_tools
            team_tools = create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                subagent_manager=self._subagent_manager,
                teammates=self.teammates,
                role=role,
                spawn_callback=None,
                event_emitter=self._event_queue.put,
                lead_name=lead_name,
            )
            tools.extend(team_tools)

            # Member 可委派子任务; Lead 通过 delegate_to_member + 任务板分配
            if role != "lead":
                from harness.tools.builtins.lead_tools import Agent_tool
                tools.append(Agent_tool(
                    manager=self._subagent_manager,
                ))

            # Lead 专属: ask_clarification
            if role == "lead":
                from harness.tools.builtins.lead_tools import ask_clarification_tool
                tools.append(ask_clarification_tool())

            teammate = TeammateAgent(
                agent_name=name,
                llm=llm,
                tools=tools,
                team_context=self.team_context,
                message_bus=self.message_bus,
                task_store=self.task_store,
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
            )
            await teammate.spawn()
            self.teammates[name] = teammate
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
                tm = await _self._create_teammate(agent_name)
                if tm:
                    tm.enable_auto_claim()
                    _self._refresh_team_context()
                    return f"Teammate '{agent_name}' spawned successfully (已开启自主认领)。"
                return f"Failed to spawn '{agent_name}': agent config not found or LLM unavailable."

            from harness.team.tools import create_team_tools
            team_tools = create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                subagent_manager=self._subagent_manager,
                teammates=self.teammates,
                role="lead",
                spawn_callback=_on_spawn,
                event_emitter=self._event_queue.put,
                lead_name=lead_name,
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
            "members": [n for n in self.teammates if n != TEAM_LEAD_NAME],
            "mode": "team",
        }

        try:
            # ── Phase 0: Triage — Lead 判断自处理还是拆解 ──
            self.tracer.trace_phase("triage")
            yield await self._emit_team_status("triage", "Lead Agent 正在分析目标...")

            # 清理上一轮遗留的团队任务 (origin=team 且非终态);
            # 用户手工创建的任务保留, 本轮会被正常调度
            stale = await self.task_store.cancel_stale_tasks()
            if stale:
                logger.info("Cancelled %d stale team tasks from previous run", len(stale))

            lead = self._get_lead()
            plan_summary = ""
            if lead:
                try:
                    # 直接发送用户消息, Lead 自行判断自处理 vs 拆解 (见 <task_triage>)
                    triage_task = TeamTask(
                        id=str(uuid.uuid4())[:8],
                        project_id=self._project_id,
                        title=f"用户目标: {message[:80]}",
                        description=message,
                        priority="high",
                    )
                    triage_accepted = await lead.assign_task(triage_task)
                    if not triage_accepted:
                        logger.warning("Lead rejected triage task (status=%s)", lead.status)
                    # 等待 Lead 完成分析 (最多 120s), 期间持续发布进度
                    for i in range(240):  # 240 * 0.5s = 120s
                        if lead.status == TeammateStatus.IDLE:
                            break
                        if lead.status == TeammateStatus.FAILED or lead.last_error:
                            yield await self._emit_team_error(
                                f"Lead Agent 分析失败: {lead.last_error or '未知错误'}")
                            break
                        # drain event queue
                        while not self._event_queue.empty():
                            yield self._event_queue.get_nowait()
                        await asyncio.sleep(0.5)

                    if lead.last_error:
                        logger.warning("Lead '%s' triage error: %s", lead.name, lead.last_error)
                except Exception as exc:
                    logger.warning("Lead triage failed: %s, continuing", exc)

            # ── 判断: 自处理 or 拆解? ──
            all_tasks = await self.task_store.load_tasks()
            # 子任务 = 非 "用户目标:" 标题的任务 (由 Lead 的 task_create 产生)
            sub_tasks = [t for t in all_tasks if not t.title.startswith("用户目标:")]
            has_sub_tasks = len(sub_tasks) > 0

            if not has_sub_tasks:
                if lead and lead.last_error:
                    # Lead 失败 → 降级为自动分配
                    logger.warning("Lead triage failed — using fallback plan")
                    yield await self._emit_team_status(
                        "triage",
                        "Lead 分析失败 (LLM 可能不可用)，自动降级为简单任务拆分。")
                    workers = [name for name in self.teammates
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
                    yield await self._emit_team_status("triage", "Lead Agent 决定独立完成此任务。")
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

            # ── 开启 Member 自主认领 (仅拆解模式) ──
            if has_sub_tasks:
                for name, tm in self.teammates.items():
                    if tm._role != "lead":
                        tm.enable_auto_claim()
                logger.info("Auto-claim enabled for all members after triage")

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

                        # ── 事件驱动: 等待进展, 不再 sleep() ──
                        dispatched = await self._dispatch_ready_tasks()

                        # ── 处理 event queue (SSE 输出) ──
                        while not self._event_queue.empty():
                            yield self._event_queue.get_nowait()

                        # ── Teammate 完成通知: 让 Lead 知道进展 (去重) ──
                        for name, tm in self.teammates.items():
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

                # ── Phase 3: Lead LLM Synthesis ──
                self.tracer.trace_phase("synthesizing")
                yield await self._emit_team_status("synthesizing", "Lead Agent 正在汇总结果...")
                async for event in self._llm_synthesize(lead, plan_summary):
                    yield event

        except Exception as exc:
            logger.exception("Team execution failed")
            yield await self._emit_team_error(f"Team 执行失败: {exc}")

        finally:
            # ── Tracing: 记录结束状态 ──
            final_status = "completed" if await self._is_complete() else ("cancelled" if self._cancelled else "error")
            self.tracer.trace_team_end(status=final_status, total_rounds=self._round)
            self.tracer.shutdown()

            # 清理: shutdown 所有 teammate (带超时, 防止文件 I/O 阻塞)
            _shutdown_timeout = 10  # 每个 teammate 最多等待 10 秒
            for tm in self.teammates.values():
                if tm.status not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                    try:
                        await asyncio.wait_for(tm.shutdown(), timeout=_shutdown_timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Teammate '%s' shutdown timed out after %ds — forcing exit",
                            tm.name, _shutdown_timeout,
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
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch_ready_tasks(self) -> int:
        """分配就绪任务给 IDLE teammate. 返回 dispatch 数量.

        分配策略:
        - task.assigned_agent 已指定 → 优先尊重 (Lead 的领域判断)
        - 未指定 → 领域匹配 (选匹配分最高的空闲成员)
        - 领域匹配失败 → 负载均衡兜底 (保证任务不卡住)

        高可用语义:
        - 指定成员已退出 (SHUTDOWN/FAILED) → 任务收回公共池, 按未指定策略重新分配;
        - 指定成员正忙 (WORKING) → 跳过, 留待下轮;
        - 受理失败 (竞态) → _assign_task_to_teammate 内部回滚任务为 PENDING.
        """
        ready_tasks = await self.task_store.get_ready_tasks()
        dispatched = 0

        for task in ready_tasks:
            if task.status != TeamTaskStatus.PENDING:
                continue

            tm: TeammateAgent | None = None
            if task.assigned_agent:
                tm = self.teammates.get(task.assigned_agent)
                if tm is None or tm.status in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                    # 指定成员不可用 → 收回任务到公共池重新分配
                    logger.warning(
                        "Task '%s' assigned to unavailable teammate '%s' — returning to pool",
                        task.id, task.assigned_agent,
                    )
                    await self.task_store.update_task(task.id, assigned_agent=None)
                    task.assigned_agent = None
                    tm = self._select_best_match_teammate(task)
                elif tm.status != TeammateStatus.IDLE:
                    continue  # 成员正忙, 留待下轮
            else:
                tm = self._select_best_match_teammate(task)

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
            tm.name, "busy", task_id=task.id, task_title=task.title))
        return True

    def _select_best_match_teammate(self, task: TeamTask) -> TeammateAgent | None:
        """为未指定分配对象的任务选择最匹配的空闲成员.

        策略 (两级):
        1. 领域匹配: 对每个空闲成员加载 AgentCard, 计算与任务的匹配分,
           选最高分 (需 ≥ CLAIM_THRESHOLD=50, 即 ≥2 个工具命中)
        2. 负载均衡兜底: 匹配分都不够 → 选已完成任务最少的空闲成员
        """
        # 收集空闲成员 (排除 Lead)
        idle_members = [
            (name, tm) for name, tm in self.teammates.items()
            if tm.status == TeammateStatus.IDLE
            and tm.last_error is None
            and name != TEAM_LEAD_NAME
        ]
        if not idle_members:
            return None

        # ── 领域匹配 ──
        from harness.team.agent_card import get_card, compute_card_task_match

        scored: list[tuple[float, int, str, TeammateAgent]] = []
        for name, tm in idle_members:
            card = get_card(self._project_id, name, user_id=self._user_id)
            if card is not None:
                score = compute_card_task_match(card, task.title, task.description)
                if score >= 50:  # CLAIM_THRESHOLD
                    scored.append((score, tm.completed_tasks, name, tm))
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
            return best[3]

        # ── 兜底: 负载均衡 ──
        return self._select_idle_teammate()

    def _select_idle_teammate(self) -> TeammateAgent | None:
        """选择空闲且健康的 teammate (负载均衡: 已完成任务少的优先)."""
        idle = [(tm.completed_tasks, name, tm)
                for name, tm in self.teammates.items()
                if tm.status == TeammateStatus.IDLE and tm.last_error is None]
        if not idle:
            return None
        idle.sort(key=lambda x: x[0])
        return idle[0][2]

    def _refresh_team_context(self) -> None:
        """刷新 TeamContext.members 为实时快照 (含动态 spawn 的新成员).

        TeammateAgent 每个工作周期重建 system prompt, 成员状态/名单以这里为准.
        """
        if self.team_context is None:
            return
        self.team_context.members = [tm.to_runtime() for tm in self.teammates.values()]

    def _get_lead(self) -> TeammateAgent | None:
        """获取平台内置 Lead Agent — 通过 team_context.lead_name 查找."""
        lead_name = self.team_context.lead_name if self.team_context else ""
        if lead_name and lead_name in self.teammates:
            return self.teammates[lead_name]
        # 安全网: 异常场景下返回第一个 teammate (不应正常触发)
        for tm in self.teammates.values():
            return tm
        return None

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

    async def _llm_synthesize(
        self, lead: TeammateAgent | None, plan_summary: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """让 Lead LLM 智能汇总所有任务结果 (替代静态 dump)."""
        all_tasks = await self.task_store.load_tasks()
        completed = [t for t in all_tasks if t.status == TeamTaskStatus.COMPLETED
                     and not t.title.startswith("规划:") and not t.title.startswith("用户目标:")]
        # 失败汇总含级联取消的任务 (error 中带依赖失败原因)
        failed = [t for t in all_tasks if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)]

        if not completed and not failed:
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": "Team 执行完成, 但没有任何任务产出结果。", "msg_type": "text"}
            return

        if lead is None or lead.status == TeammateStatus.FAILED:
            # fallback: 静态汇总
            if completed:
                parts = [f"## {t.title}\n**执行者**: {t.assigned_agent or '未知'}\n\n{t.output or '(无输出)'}"
                         for t in completed]
                yield {"type": "message", "thread_id": self._thread_id,
                       "content": f"# Team 执行结果\n\n" + "\n\n---\n\n".join(parts),
                       "msg_type": "text"}
            if failed:
                failed_list = "\n".join(
                    f"- {t.title} ({t.assigned_agent or '未知'}): {t.error or '未知错误'}"
                    for t in failed)
                yield {"type": "message", "thread_id": self._thread_id,
                       "content": f"# 失败任务\n\n{failed_list}", "msg_type": "error"}
            return

        # ── 构建汇总任务给 Lead ──
        completed_summaries = "\n".join(
            f"- [{t.id}] {t.title} (执行者: {t.assigned_agent or '未知'})\n"
            f"  输出: {t.output[:300] if t.output else '(无输出)'}"
            for t in completed
        )
        failed_summaries = "\n".join(
            f"- [{t.id}] {t.title} (执行者: {t.assigned_agent or '未知'}): {t.error or '未知错误'}"
            for t in failed
        ) if failed else "无"

        synthesis_task = TeamTask(
            id=str(uuid.uuid4())[:8],
            project_id=self._project_id,
            title=f"汇总: 团队执行结果",
            description=(
                f"你是一个 Team 的 Lead Agent。请汇总以下团队执行结果, 生成最终报告。\n\n"
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
        accepted = await lead.assign_task(synthesis_task)
        if not accepted:
            # Lead 不可受理 (竞态/异常状态) → 静态汇总兜底, 保证用户一定拿得到结果
            logger.warning("Lead rejected synthesis task (status=%s) — 静态汇总兜底", lead.status)
            async for event in self._synthesize_results():
                yield event
            return

        # 等待 Lead 完成汇总 (最多 30s)
        for i in range(60):  # 60 * 0.5s = 30s
            if lead.status == TeammateStatus.IDLE:
                break
            if lead.status == TeammateStatus.FAILED or lead.last_error:
                break
            while not self._event_queue.empty():
                yield self._event_queue.get_nowait()
            await asyncio.sleep(0.5)

        # drain 最后的 event queue
        while not self._event_queue.empty():
            yield self._event_queue.get_nowait()

    async def _synthesize_results(self) -> AsyncIterator[dict[str, Any]]:
        """静态汇总 (保留作为 fallback, _llm_synthesize 内部已包含)."""
        all_tasks = await self.task_store.load_tasks()
        completed = [t for t in all_tasks if t.status == TeamTaskStatus.COMPLETED]
        failed = [t for t in all_tasks if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)]

        if completed:
            parts = [f"## {t.title}\n**执行者**: {t.assigned_agent or '未知'}\n\n{t.output or '(无输出)'}"
                     for t in completed]
            yield {"type": "message", "thread_id": self._thread_id,
                   "content": f"# Team 执行结果\n\n" + "\n\n---\n\n".join(parts),
                   "msg_type": "text"}

        if failed:
            failed_list = "\n".join(
                f"- {t.title} ({t.assigned_agent or '未知'}): {t.error or '未知错误'}"
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
        """取消所有运行中的 teammate."""
        self._cancelled = True
        for tm in self.teammates.values():
            if tm.status == TeammateStatus.WORKING:
                await tm.shutdown()

    async def _reap_crashed_teammates(self) -> None:
        """回收崩溃成员: 状态 WORKING 但 agent loop 已终止.

        将其标记为 FAILED 并从总线注销; 其手上的 IN_PROGRESS 任务回收为未分配
        PENDING (retry_count+1), 交给其他成员有界重试; 超过 max_retries 则置 FAILED.
        """
        for tm in self.teammates.values():
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
                    busy = sum(1 for tm in self.teammates.values()
                               if tm.status == TeammateStatus.WORKING)
                    idle = sum(1 for tm in self.teammates.values()
                               if tm.status == TeammateStatus.IDLE)
                    # 无进展且无推进可能: 无人在干活, 且 (无就绪任务 或 有就绪任务但无人能接)
                    if busy == 0 and (not ready or idle == 0):
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
            "started_at": _now_iso() if status == "busy" else "",
        }
