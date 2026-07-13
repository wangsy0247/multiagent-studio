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
from pathlib import Path
from typing import Any, AsyncIterator

from harness.config.agents_config import load_agent_config
from harness.config.paths import get_paths
from harness.models import SubagentStatus
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
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_host: str = "",
    ) -> None:
        self._project_id = project_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._subagent_manager = subagent_manager
        self._skill_storage = skill_storage

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
        self.tracer = TeamTracer(
            trace_dir=trace_dir,
            session_id=thread_id,
            user_id=user_id,
            public_key=langfuse_public_key or None,
            secret_key=langfuse_secret_key or None,
            host=langfuse_host or None,
        )

        # ── 去重: 已通知过 Lead 的空闲 teammate ──
        self._notified_idle: set[str] = set()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """加载项目、创建 TeammateAgent 池."""
        project = await _load_project_json(self._project_id, self._user_id)
        if project is None:
            raise ValueError(f"Project '{self._project_id}' not found")

        project_name = project.get("name", self._project_id)
        project_description = project.get("description", "")
        member_names: list[str] = project.get("members", [])

        if not member_names:
            logger.warning("Project '%s' has no members — team mode will degrade", self._project_id)

        # ── 构建 TeamContext ──
        member_runtimes: list[TeamMemberRuntime] = []
        for name in member_names:
            cfg = load_agent_config(name, user_id=self._user_id)
            if cfg is None and self._user_id != "default":
                cfg = load_agent_config(name, user_id="default")
            role = "lead" if (cfg and cfg.can_be_lead) else "member"
            member_runtimes.append(TeamMemberRuntime(
                agent_name=name, role=role, status=TeammateStatus.SPAWNING,
            ))

        self.team_context = TeamContext(
            project_id=self._project_id,
            project_name=project_name,
            project_description=project_description,
            thread_id=self._thread_id,
            user_id=self._user_id,
            members=member_runtimes,
        )

        # ── 为每个 member 创建 TeammateAgent ──
        for name in member_names:
            await self._create_teammate(name)

        logger.info(
            "TeamOrchestrator initialized: project=%s teammates=%d",
            self._project_id, len(self.teammates),
        )

    async def _create_teammate(self, name: str) -> TeammateAgent | None:
        """创建并 spawn 一个 TeammateAgent."""
        try:
            cfg = load_agent_config(name, user_id=self._user_id)
            if cfg is None and self._user_id != "default":
                cfg = load_agent_config(name, user_id="default")

            llm = self._llm_factory(cfg.model if cfg and cfg.model != "inherit" else None) if self._llm_factory else None
            if llm is None:
                logger.error("No LLM available for teammate '%s'", name)
                return None

            tools: list = []
            if self._tool_registry:
                if cfg and cfg.tool_groups:
                    for group in cfg.tool_groups:
                        tools.extend(self._tool_registry.get_tools_by_category(group))
                else:
                    tools = list(self._tool_registry.get_core_tools())

            # 注入 team 工具 (按角色过滤)
            role = "lead" if (cfg and cfg.can_be_lead) else "member"
            lead_name = self._get_lead_name_from_config()

            #  spawn callback: 让 Lead 可以动态创建 teammate
            _self = self

            async def _on_spawn(agent_name: str) -> str:
                tm = await _self._create_teammate(agent_name)
                if tm:
                    # 新 teammate 立即可认领任务
                    tm.enable_auto_claim()
                    return f"Teammate '{agent_name}' spawned successfully (已开启自主认领)。"
                return f"Failed to spawn '{agent_name}': agent config not found or LLM unavailable."

            from harness.team.tools import create_team_tools
            team_tools = create_team_tools(
                task_store=self.task_store,
                message_bus=self.message_bus,
                subagent_manager=self._subagent_manager,
                teammates=self.teammates,
                role=role,
                spawn_callback=_on_spawn if role == "lead" else None,
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
            )
            await teammate.spawn()
            self.teammates[name] = teammate
            logger.info("Teammate '%s' created and spawned (role=%s)", name, role)
            return teammate

        except Exception as exc:
            logger.error("Failed to create teammate '%s': %s", name, exc)
            return None

    def _get_lead_name_from_config(self) -> str | None:
        """从已加载的 teammate 中查找 Lead 名称."""
        for name, tm in self.teammates.items():
            if tm._agent_config and tm._agent_config.can_be_lead:
                return name
        # fallback: 第一个 teammate
        for name in self.teammates:
            return name
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
            # ── Phase 1: Lead Planning ──
            self.tracer.trace_phase("planning")
            yield await self._emit_team_status("planning", "Lead Agent 正在分析目标并拆解任务...")

            await self.task_store.clear_all()

            lead = self._get_lead()
            plan_summary = ""
            if lead:
                try:
                    plan_task = TeamTask(
                        id=str(uuid.uuid4())[:8],
                        project_id=self._project_id,
                        title=f"规划: {message[:80]}",
                        description=(
                            f"你是一个 Team 的 Lead Agent。分析以下用户目标并拆解为子任务:\n\n"
                            f"【用户目标】\n{message}\n\n"
                            f"【Team 成员】\n"
                            + "\n".join(f"- {n}" for n in self.teammates)
                            + "\n\n"
                            f"请使用 task_create 工具逐个创建子任务 (不要指定 assigned_agent, 让成员自主认领)。\n"
                            f"创建完所有子任务后, 使用 task_update 将当前「规划:」任务标记为 completed。\n"
                            f"注意: 不要只是输出文本, 必须调用 task_create 工具!"
                        ),
                        priority="high",
                    )
                    await lead.assign_task(plan_task)
                    # 等待 lead 完成规划 (最多 60s), 期间持续发布进度
                    for i in range(120):  # 120 * 0.5s = 60s
                        if lead.status == TeammateStatus.IDLE:
                            break
                        if lead.status == TeammateStatus.FAILED or lead.last_error:
                            yield await self._emit_team_error(
                                f"Lead Agent 规划失败: {lead.last_error or '未知错误'}")
                            break
                        # drain event queue + 进度事件
                        while not self._event_queue.empty():
                            yield self._event_queue.get_nowait()
                        if i % 10 == 0:  # 每 5s 发一次进度
                            yield await self._emit_team_status(
                                "planning",
                                f"Lead Agent 正在规划中... (已等待 {i * 0.5:.0f}s, 状态: {lead.status.value})")
                        await asyncio.sleep(0.5)

                    # 检查规划结果
                    if lead.last_error:
                        logger.warning("Lead '%s' planning error: %s", lead.name, lead.last_error)
                    completed = [t for t in await self.task_store.load_tasks()
                                 if t.status == TeamTaskStatus.COMPLETED]
                    if completed:
                        plan_summary = completed[0].output or ""
                        yield {
                            "type": "message",
                            "thread_id": self._thread_id,
                            "content": f"📋 **Lead Agent 规划结果**\n\n{plan_summary}",
                            "msg_type": "text",
                            "subagent_name": lead.name,
                        }
                    elif lead.last_error:
                        plan_summary = f"[规划失败] {lead.last_error}"
                except Exception as exc:
                    logger.warning("Lead planning failed: %s, continuing", exc)

            # ── Fallback: Lead 规划失败 / LLM 不可用 → 自动降级 ──
            all_tasks_after_planning = await self.task_store.load_tasks()
            has_sub_tasks = any(not t.title.startswith("规划:") for t in all_tasks_after_planning)

            if not has_sub_tasks:
                logger.warning("Lead planning produced no sub-tasks — using fallback plan")
                yield await self._emit_team_status(
                    "planning",
                    "Lead 规划未产出子任务 (LLM 可能不可用)，自动降级为简单任务拆分。")

                # 从成员列表中选一个非 Lead 的 member 来执行
                workers = [name for name in self.teammates
                          if name != (lead.name if lead else "")]
                assigned = workers[0] if workers else next(iter(self.teammates), None)

                await self.task_store.create_task(
                    title=message[:100],
                    description=message,
                    assigned_agent=assigned,
                    priority="high",
                )
                plan_summary = f"[自动降级] 将用户目标直接创建为任务，分配给 {assigned}"
                logger.info("Fallback: created single task assigned to '%s'", assigned)

            # 创建用户目标根任务 (仅用于汇总, 不分配给 teammate)
            root_task = await self.task_store.create_task(
                title=f"用户目标: {message[:100]}",
                description=(
                    f"{message}\n\n"
                    + (f"【Lead 规划方案】\n{plan_summary}" if plan_summary else "")
                ),
                priority="high",
            )
            await self.task_store.update_task(root_task.id, status=TeamTaskStatus.COMPLETED)

            # ── 规划完成 → 开启 Member 自主认领 ──
            for name, tm in self.teammates.items():
                if tm._role != "lead":
                    tm.enable_auto_claim()
            logger.info("Auto-claim enabled for all members after planning")

            # ── Phase 2: Event-Driven Dispatch Loop ──
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
                            lead = self._get_lead()
                            if lead and lead.name != name:
                                await self.message_bus.send(TeamMessage(
                                    from_agent=name, to_agent=lead.name,
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
        """获取 Lead Agent."""
        for name, tm in self.teammates.items():
            if tm._agent_config and tm._agent_config.can_be_lead:
                return tm
        # fallback: 第一个 teammate
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
