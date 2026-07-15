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
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from harness.config.agents_config import load_agent_config
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
MAX_RETRIES = 3
OVERALL_TIMEOUT = 1800         # Team 整体超时 30 分钟
DEADLOCK_TIMEOUT = 120         # 死锁 2 分钟无进展
MAX_TEAM_ROUNDS = 100          # 最大调度轮次


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_project_json(project_id: str, user_id: str) -> dict[str, Any] | None:
    """加载项目 JSON 文件."""
    paths = get_paths()
    proj_path = paths.base_dir / "users" / user_id / "projects" / f"{project_id}.json"
    if not proj_path.exists():
        return None
    with open(proj_path) as f:
        return json.load(f)


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
    ) -> None:
        self._project_id = project_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._subagent_manager = subagent_manager
        self._skill_storage = skill_storage
        self._effective_config = effective_config  # Lead's EffectiveConfig

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

        # ── SSE 事件队列 ──
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # ── Tracing ──
        from harness.config.paths import get_paths
        trace_dir = get_paths().base_dir / "users" / user_id / "team_traces" / project_id / thread_id
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
        1. 加载项目 JSON → 获取成员列表
        2. 解析 Lead 身份 (can_be_lead 标记 或 第一个成员)
        3. 生成所有成员的 agent-card.json — 注入 TeamContext
        4. 创建 Lead Agent (default 配置 + 系统 Lead SOUL)
        5. 创建 Member Agent (各自的 L0+L1+L2 配置)
        """
        project = await _load_project_json(self._project_id, self._user_id)
        if project is None:
            raise ValueError(f"Project '{self._project_id}' not found")

        project_name = project.get("name", self._project_id)
        project_description = project.get("description", "")
        member_names: list[str] = project.get("members", [])

        if not member_names:
            logger.warning("Project '%s' has no members — team mode will degrade", self._project_id)

        # ── 1. 解析 Lead 身份 ──
        lead_name = self._resolve_lead_identity(member_names)

        # ── 2. 构建 TeamMemberRuntime 列表 (Lead 排第一) ──
        member_runtimes: list[TeamMemberRuntime] = []
        # Lead 先添加
        if lead_name:
            member_runtimes.append(TeamMemberRuntime(
                agent_name=lead_name, role="lead", status=TeammateStatus.SPAWNING,
            ))
        for name in member_names:
            if name != lead_name:
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

        # ── 3. 生成 agent cards (在创建 TeammateAgent 之前) ──
        from harness.team.agent_card import generate_agent_card, save_project_cards
        from harness.config.config_loader import ConfigLoader as _CL

        cards: dict[str, Any] = {}
        for name in member_names:
            try:
                role = "lead" if name == lead_name else "member"
                member_eff = _CL.load_effective(user_id=self._user_id, agent_name=name)
                card = generate_agent_card(
                    name,
                    user_id=self._user_id,
                    tool_registry=self._tool_registry,
                    skill_storage=self._skill_storage,
                    effective_config=member_eff,
                    role=role,
                )
                cards[name] = card
            except Exception as exc:
                logger.warning("Failed to generate agent card for '%s': %s", name, exc)

        # 全量写入单个 agent_card.json
        if cards:
            save_project_cards(self._project_id, cards, user_id=self._user_id)

        # 注入 TeamContext — 后续 TeammateAgent 构建 system prompt 时可用
        if cards:
            self.team_context.set_team_capabilities(cards)
            logger.info("Agent cards generated: %d members", len(cards))
        else:
            logger.warning("No agent cards generated — team capabilities unavailable")

        # ── 4. 创建 Lead Agent ──
        failed_members: list[str] = []
        if lead_name:
            lead = await self._create_lead(lead_name)
            if lead is None:
                failed_members.append(lead_name)
        else:
            lead = None

        # ── 5. 创建 Member Agent ──
        for name in member_names:
            if name == lead_name:
                continue
            tm = await self._create_teammate(name)
            if tm is None:
                failed_members.append(name)

        # ── 检查 ──
        if not self.teammates:
            raise ValueError(
                f"Team 初始化失败: 所有成员 ({', '.join(member_names)}) 都无法创建。"
                f" 请检查每个 Agent 的配置 (model/api_key) 是否正确。"
            )

        if failed_members:
            logger.warning(
                "TeamOrchestrator: %d/%d members failed to create: %s",
                len(failed_members), len(member_names), ", ".join(failed_members),
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
            "TeamOrchestrator initialized: project=%s teammates=%d/%d lead=%s",
            self._project_id, len(self.teammates), len(member_names), lead_name,
        )

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
            )
            tools.extend(team_tools)

            # Member 可委派子任务; Lead 通过 delegate_to_member + 任务板分配
            if role != "lead":
                from harness.tools.builtins.lead_tools import task_tool
                tools.append(task_tool(
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
                tracer=self.tracer,
                effective_config=member_eff,
            )
            await teammate.spawn()
            self.teammates[name] = teammate
            logger.info("Teammate '%s' created and spawned (role=%s)", name, role)
            return teammate

        except Exception as exc:
            logger.error("Failed to create teammate '%s': %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Lead 身份解析 & 创建
    # ------------------------------------------------------------------

    def _resolve_lead_identity(self, member_names: list[str]) -> str:
        """解析 Lead 身份.

        优先级:
        1. 第一个 can_be_lead=True 的 member
        2. 回退: 第一个 member
        3. 无 member → 空字符串
        """
        for name in member_names:
            cfg = load_agent_config(name, user_id=self._user_id)
            if cfg is None and self._user_id != "default":
                cfg = load_agent_config(name, user_id="default")
            if cfg and cfg.can_be_lead:
                logger.info("Lead identity resolved: '%s' (can_be_lead=True)", name)
                return name
        # Fallback
        if member_names:
            logger.info("Lead identity resolved: '%s' (first member)", member_names[0])
            return member_names[0]
        return ""

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

    async def _create_lead(self, lead_name: str) -> TeammateAgent | None:
        """创建 Lead Agent — 使用 default 配置 + 系统 Lead SOUL.

        Lead 的 LLM 配置 (api_key, base_url, model, temperature, max_tokens)
        来自用户的全局默认配置 (agent_name="default"), 而非某个特定 member 的配置。
        SOUL 由系统生成, 不需要用户手写 SOUL.md。

        Args:
            lead_name: Lead 的标识名称 (来自项目成员列表)
        """
        try:
            from harness.config.config_loader import ConfigLoader

            # ── 使用 default agent 的 EffectiveConfig ──
            lead_eff = ConfigLoader.load_effective(
                user_id=self._user_id, agent_name="default",
            )
            logger.info(
                "Creating Lead '%s' with default config: model=%s tool_groups=%s",
                lead_name, lead_eff.model, lead_eff.tool_groups,
            )

            # ── LLM ──
            llm = self._llm_factory(
                lead_eff.model,
                temperature=lead_eff.temperature,
                max_tokens=lead_eff.max_tokens,
            ) if self._llm_factory else None
            if llm is None:
                logger.error("No LLM available for Lead '%s'", lead_name)
                return None

            # ── 工具: default agent 的 tool_groups + team tools ──
            tools: list = []
            if self._tool_registry:
                for group in lead_eff.tool_groups:
                    tools.extend(self._tool_registry.get_tools_by_category(group))

            # 注入 team 工具 (Lead 角色)
            _self = self

            async def _on_spawn(agent_name: str) -> str:
                tm = await _self._create_teammate(agent_name)
                if tm:
                    tm.enable_auto_claim()
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
                tracer=self.tracer,
                effective_config=lead_eff,
                soul_override=lead_soul,
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
            "members": list(self.teammates.keys()),
            "mode": "team",
        }

        try:
            # ── Phase 0: Triage — Lead 判断自处理还是拆解 ──
            self.tracer.trace_phase("triage")
            yield await self._emit_team_status("triage", "Lead Agent 正在分析目标...")

            await self.task_store.clear_all()

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
                    await lead.assign_task(triage_task)
                    # 等待 Lead 完成分析 (最多 120s), 期间持续发布进度
                    for i in range(240):  # 240 * 0.5s = 120s
                        if lead.status == TeammateStatus.IDLE:
                            break
                        if lead.status == TeammateStatus.FAILED or lead.last_error:
                            yield await self._emit_team_error(
                                f"Lead Agent 分析失败: {lead.last_error or '未知错误'}")
                            break
                        # drain event queue + 进度事件
                        while not self._event_queue.empty():
                            yield self._event_queue.get_nowait()
                        if i % 20 == 0:  # 每 10s 发一次进度
                            yield await self._emit_team_status(
                                "triage",
                                f"Lead Agent 正在分析目标... (状态: {lead.status.value})")
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

            # ── 开启 Member 自主认领 ──
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
                                lead_notify = self._get_lead()
                                if lead_notify and lead_notify.name != name:
                                    await self.message_bus.send(TeamMessage(
                                        from_agent=name, to_agent=lead_notify.name,
                                        msg_type=TeamMessageType.LIFECYCLE,
                                        content=f"已完成 {tm.completed_tasks} 个任务, 等待新任务",
                                    ))
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

            # 清理: shutdown 所有 teammate
            for tm in self.teammates.values():
                if tm.status not in (TeammateStatus.SHUTDOWN, TeammateStatus.FAILED):
                    await tm.shutdown()

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
        """分配就绪任务给 IDLE teammate. 返回 dispatch 数量."""
        ready_tasks = await self.task_store.get_ready_tasks()
        dispatched = 0

        for task in ready_tasks:
            if task.status != TeamTaskStatus.PENDING:
                continue

            if task.assigned_agent:
                tm = self.teammates.get(task.assigned_agent)
                if tm and tm.status == TeammateStatus.IDLE:
                    await self._assign_task_to_teammate(tm, task)
                    dispatched += 1
            else:
                tm = self._select_idle_teammate()
                if tm:
                    await self.task_store.update_task(
                        task.id,
                        assigned_agent=tm.name,
                        status=TeamTaskStatus.IN_PROGRESS,
                    )
                    await self._assign_task_to_teammate(tm, task)
                    dispatched += 1

        return dispatched

    async def _assign_task_to_teammate(self, tm: TeammateAgent, task: TeamTask) -> None:
        """分配任务给 teammate 并触发状态更新."""
        await self.task_store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
        await tm.assign_task(task)
        self._progress_event.set()

    def _select_idle_teammate(self) -> TeammateAgent | None:
        """选择空闲且健康的 teammate (负载均衡: 已完成任务少的优先)."""
        idle = [(tm.completed_tasks, name, tm)
                for name, tm in self.teammates.items()
                if tm.status == TeammateStatus.IDLE and tm.last_error is None]
        if not idle:
            return None
        idle.sort(key=lambda x: x[0])
        return idle[0][2]

    def _get_lead(self) -> TeammateAgent | None:
        """获取 Lead Agent — 优先用 team_context.lead_name 查找."""
        lead_name = self.team_context.lead_name if self.team_context else ""
        if lead_name and lead_name in self.teammates:
            return self.teammates[lead_name]
        # fallback: 扫描 can_be_lead 标记
        for name, tm in self.teammates.items():
            eff = tm._effective_config
            if eff and eff.can_be_lead:
                return tm
            if tm._agent_config and tm._agent_config.can_be_lead:
                return tm
        # 最后 fallback: 第一个 teammate
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
        failed = [t for t in all_tasks if t.status == TeamTaskStatus.FAILED]

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
        await lead.assign_task(synthesis_task)

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
        failed = [t for t in all_tasks if t.status == TeamTaskStatus.FAILED]

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

    async def _watchdog(self) -> None:
        """后台看门狗: 整体超时、死锁检测."""
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

            # 死锁检测
            if self._last_progress_at:
                since = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(self._last_progress_at)).total_seconds()
                if since > DEADLOCK_TIMEOUT:
                    ready = await self.task_store.get_ready_tasks()
                    busy = sum(1 for tm in self.teammates.values()
                               if tm.status == TeammateStatus.WORKING)
                    if not ready and busy == 0:
                        logger.warning("Watchdog: deadlock detected")
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
