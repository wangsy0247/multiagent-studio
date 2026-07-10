"""TeamOrchestrator — Team 执行调度器。

核心调度循环:
    PLANNING → DISPATCH_LOOP → SYNTHESIZING → COMPLETED

功能:
- 加载项目配置 + 成员列表
- 为每个成员创建 MemberAgentExecutor
- 调度循环: ready_tasks → idle members → dispatch
- 监控 member 执行、处理消息、更新任务状态
- watchdog: 整体超时、死锁检测、循环检测
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from harness.config.agents_config import load_agent_config
from harness.config.paths import get_paths
from harness.models import SubAgentConfig, SubAgentResult, SubagentStatus
from harness.team.context import TeamContext
from harness.team.message_bus import TeamMessageBus
from harness.team.models import (
    TeamMemberRuntime,
    TeamMessage,
    TeamMessageType,
    TeamTask,
    TeamTaskStatus,
)
from harness.team.task_store import TeamTaskStore
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── 常量 ──
MAX_RETRIES = 3                # 单个任务最大重试次数
MAX_TEAM_ROUNDS = 100          # Team 整体最大轮次
OVERALL_TIMEOUT = 1800         # Team 整体超时（秒，30 分钟）
DEADLOCK_TIMEOUT = 120         # 死锁超时（秒，2 分钟无进展）
TASK_RETRY_DELAY = 2.0         # 任务重试前等待（秒）


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
    """Team 执行编排器。

    使用方式:
        orchestrator = TeamOrchestrator(...)
        await orchestrator.initialize()
        async for event in orchestrator.run(message):
            # yield SSE events
    """

    def __init__(
        self,
        project_id: str,
        thread_id: str,
        user_id: str,
        *,
        llm_factory: Any = None,              # Callable[[str | None], BaseChatModel]
        tool_registry: ToolRegistry | None = None,
        subagent_manager: Any = None,          # SubagentManager
        skill_storage: Any = None,
    ) -> None:
        self._project_id = project_id
        self._thread_id = thread_id
        self._user_id = user_id

        self._llm_factory = llm_factory
        self._tool_registry = tool_registry
        self._subagent_manager = subagent_manager
        self._skill_storage = skill_storage

        # ── 核心组件（initialize 中创建）──
        self.task_store: TeamTaskStore = TeamTaskStore(project_id, user_id)
        self.message_bus: TeamMessageBus = TeamMessageBus(project_id, user_id)
        self.team_context: TeamContext | None = None
        self.members: dict[str, TeamMemberRuntime] = {}     # agent_name → runtime
        self._member_executors: dict[str, Any] = {}         # agent_name → MemberAgentExecutor

        # ── 调度状态 ──
        self._round: int = 0
        self._cancelled: bool = False
        self._started_at: str = ""
        self._last_progress_at: str = ""

        # ── 事件队列（SSE 输出）──
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """加载项目配置、解析成员、构建 executor."""
        # 1. 加载项目
        project = await _load_project_json(self._project_id, self._user_id)
        if project is None:
            raise ValueError(f"Project '{self._project_id}' not found")

        project_name = project.get("name", self._project_id)
        project_description = project.get("description", "")
        member_names: list[str] = project.get("members", [])

        if not member_names:
            logger.warning("Project '%s' has no members — team mode will degrade", self._project_id)

        # 2. 初始化成员运行时状态
        self.members = {}
        for name in member_names:
            cfg = load_agent_config(name, user_id=self._user_id)
            role = "lead" if (cfg and cfg.can_be_lead) else "member"
            self.members[name] = TeamMemberRuntime(
                agent_name=name,
                role=role,
                status="idle",
            )

        # 3. 构建 TeamContext
        self.team_context = TeamContext(
            project_id=self._project_id,
            project_name=project_name,
            project_description=project_description,
            thread_id=self._thread_id,
            user_id=self._user_id,
            members=list(self.members.values()),
        )

        # 4. 为每个 member 创建 MemberAgentExecutor
        for name in member_names:
            try:
                cfg = load_agent_config(name, user_id=self._user_id)
                llm = self._llm_factory(cfg.model if cfg and cfg.model != "inherit" else None)
                # 获取工具
                tools: list[BaseTool] = []
                if self._tool_registry:
                    if cfg and cfg.tool_groups:
                        for group in cfg.tool_groups:
                            tools.extend(self._tool_registry.get_tools_by_category(group))
                    else:
                        tools = list(self._tool_registry.get_core_tools())

                # 构建 parent_state
                parent_state: dict[str, Any] = {
                    "thread_id": self._thread_id,
                    "user_id": self._user_id,
                }

                from harness.team.member_executor import MemberAgentExecutor

                executor = MemberAgentExecutor(
                    agent_name=name,
                    llm=llm,
                    tools=tools,
                    team_context=self.team_context,
                    parent_state=parent_state,
                    skill_storage=self._skill_storage,
                )
                self._member_executors[name] = executor
                logger.info("Team member '%s' initialized (role=%s)", name, role)
            except Exception as exc:
                logger.error("Failed to init member '%s': %s", name, exc)
                if name in self.members:
                    self.members[name].status = "failed"
                    self.members[name].last_error = str(exc)

        logger.info(
            "TeamOrchestrator initialized: project=%s members=%d",
            self._project_id, len(self._member_executors),
        )

    # ------------------------------------------------------------------
    # 主执行循环
    # ------------------------------------------------------------------

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """执行 Team 模式的主入口 — 异步生成器 yield SSE 事件.

        Args:
            message: 用户目标消息

        Yields:
            SSE 事件字典
        """
        self._started_at = _now_iso()
        self._last_progress_at = self._started_at

        # ── 初始事件 ──
        yield {
            "type": "team_start",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "members": [m.agent_name for m in self.members.values()],
            "mode": "team",
        }

        try:
            # ── Phase 1: Planning ──
            yield await self._emit_team_status("planning", "Lead Agent 正在分析目标并拆解任务...")

            # 将用户消息作为第一个任务创建到任务板上
            plan_task = await self.task_store.create_task(
                title=f"用户目标: {message[:100]}",
                description=message,
                priority="high",
            )
            yield await self._emit_task_update(plan_task)

            # 启动 watchdog
            watchdog_task = asyncio.create_task(self._watchdog())

            try:
                # ── Phase 2: Dispatch Loop ──
                yield await self._emit_team_status("dispatching", "开始调度任务执行...")

                while not self._is_complete() and not self._cancelled:
                    self._round += 1

                    if self._round > MAX_TEAM_ROUNDS:
                        yield await self._emit_team_error("Team 执行超过最大轮次限制")
                        break

                    # 获取就绪任务
                    ready_tasks = await self.task_store.get_ready_tasks()

                    # 分配任务给空闲成员
                    dispatched = 0
                    for task in ready_tasks:
                        if task.assigned_agent:
                            # 已分配但尚未开始 → 触发执行
                            member = self.members.get(task.assigned_agent)
                            if member and member.status == "idle":
                                dispatched += 1
                                asyncio.create_task(
                                    self._run_member_task(task.assigned_agent, task)
                                )
                        else:
                            # 未分配 → 选择一个空闲成员
                            agent = self._select_idle_agent()
                            if agent:
                                dispatched += 1
                                await self.task_store.update_task(
                                    task.id,
                                    assigned_agent=agent,
                                    status=TeamTaskStatus.ASSIGNED,
                                )
                                asyncio.create_task(
                                    self._run_member_task(agent, task)
                                )

                    # 处理消息总线事件
                    for name in self.members:
                        unread = await self.message_bus.get_unread(name)
                        for msg in unread:
                            yield {"type": "team_message", **msg.model_dump()}
                        if unread:
                            await self.message_bus.mark_all_read(name)

                    # 处理 event queue
                    while not self._event_queue.empty():
                        event = self._event_queue.get_nowait()
                        yield event

                    if dispatched == 0 and not ready_tasks:
                        # 无事可做 → 等待
                        await asyncio.sleep(0.2)

                # ── Phase 3: Synthesis ──
                yield await self._emit_team_status("synthesizing", "正在汇总结果...")

            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass

        except Exception as exc:
            logger.exception("Team execution failed")
            yield await self._emit_team_error(f"Team 执行失败: {exc}")

        finally:
            # ── 终态事件 ──
            status = "completed" if self._is_complete() else "cancelled" if self._cancelled else "error"
            yield {
                "type": "team_end",
                "thread_id": self._thread_id,
                "project_id": self._project_id,
                "status": status,
                "total_rounds": self._round,
            }

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------

    async def _run_member_task(self, agent_name: str, task: TeamTask) -> None:
        """在后台执行单个 member 任务."""
        member = self.members.get(agent_name)
        if member is None:
            return

        executor = self._member_executors.get(agent_name)
        if executor is None:
            logger.error("No executor for member '%s'", agent_name)
            return

        # 更新状态
        member.status = "busy"
        member.current_task_id = task.id
        await self.task_store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)

        await self._emit_member_status(member)
        await self._emit_task_update(task)

        try:
            # 构建指令
            instruction = f"【任务】{task.title}\n\n{task.description}"

            # 注入依赖任务的结果
            if task.dependencies:
                dep_results: list[str] = []
                for dep_id in task.dependencies:
                    dep_task = await self.task_store.get_task(dep_id)
                    if dep_task and dep_task.output:
                        dep_results.append(
                            f"依赖任务 [{dep_id}] {dep_task.title} 的结果:\n{dep_task.output}"
                        )
                if dep_results:
                    instruction += "\n\n【依赖任务结果】\n" + "\n\n".join(dep_results)

            # 执行
            result: SubAgentResult = await executor.execute(
                instruction=instruction,
                task=task,
            )

            # 处理结果
            if result.status == SubagentStatus.SUCCESS:
                await self.task_store.update_task(
                    task.id,
                    status=TeamTaskStatus.COMPLETED,
                    output=result.output,
                )
                member.completed_tasks += 1
                self._last_progress_at = _now_iso()
            else:
                task.retry_count += 1
                if task.retry_count < task.max_retries:
                    # 重试
                    await self.task_store.update_task(
                        task.id,
                        status=TeamTaskStatus.PENDING,
                        error=result.error,
                        retry_count=task.retry_count,
                    )
                    logger.warning(
                        "Task '%s' failed, will retry (%d/%d): %s",
                        task.id, task.retry_count, task.max_retries, result.error,
                    )
                    await asyncio.sleep(TASK_RETRY_DELAY)
                else:
                    await self.task_store.update_task(
                        task.id,
                        status=TeamTaskStatus.FAILED,
                        error=result.error or "Max retries exceeded",
                        output=result.output,
                    )
                    member.failed_tasks += 1
                    # 通知 Lead
                    await self.message_bus.send(TeamMessage(
                        from_agent=agent_name,
                        to_agent=None,  # broadcast
                        msg_type=TeamMessageType.TASK_UPDATE,
                        content=f"任务 {task.title} 执行失败: {result.error}",
                        task_id=task.id,
                    ))

        except Exception as exc:
            logger.exception("Member '%s' task execution crashed", agent_name)
            await self.task_store.update_task(
                task.id,
                status=TeamTaskStatus.FAILED,
                error=str(exc),
            )
            member.failed_tasks += 1
            member.last_error = str(exc)
        finally:
            member.status = "idle"
            member.current_task_id = None
            member.last_heartbeat = _now_iso()
            await self._emit_member_status(member)
            # 重新读取更新后的任务
            updated_task = await self.task_store.get_task(task.id)
            if updated_task:
                await self._emit_task_update(updated_task)

    # ------------------------------------------------------------------
    # 调度辅助
    # ------------------------------------------------------------------

    def _select_idle_agent(self) -> str | None:
        """选择一个空闲成员（优先选择已完成任务少的）."""
        idle = [
            (name, m)
            for name, m in self.members.items()
            if m.status == "idle" and name in self._member_executors
        ]
        if not idle:
            return None
        # 按已完成任务数升序排列（负载均衡）
        idle.sort(key=lambda item: item[1].completed_tasks)
        return idle[0][0]

    def _is_complete(self) -> bool:
        """所有任务是否都到达终态."""
        # 如果有任何 member 正在执行，不算完成
        for m in self.members.values():
            if m.status == "busy":
                return False
        return True

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        """取消所有运行中的 member agent."""
        self._cancelled = True
        for name, member in self.members.items():
            if member.status == "busy":
                executor = self._member_executors.get(name)
                if executor and hasattr(executor, "_current_result"):
                    # 通过 SubagentExecutor 的 cancel 机制
                    pass
                member.status = "idle"
                member.current_task_id = None

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """后台看门狗：整体超时、死锁检测."""
        while True:
            await asyncio.sleep(5)

            if self._cancelled:
                return

            # 整体超时
            if self._started_at:
                elapsed = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(self._started_at)
                ).total_seconds()
                if elapsed > OVERALL_TIMEOUT:
                    logger.warning("Team watchdog: overall timeout (%.0fs)", elapsed)
                    await self._event_queue.put(await self._emit_team_error(
                        f"Team 执行超过整体超时限制 ({OVERALL_TIMEOUT}s)"
                    ))
                    self._cancelled = True
                    return

            # 死锁检测
            if self._last_progress_at:
                since_progress = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(self._last_progress_at)
                ).total_seconds()
                if since_progress > DEADLOCK_TIMEOUT:
                    # 检查是否真的没有进展
                    ready = await self.task_store.get_ready_tasks()
                    busy = sum(1 for m in self.members.values() if m.status == "busy")
                    if not ready and busy == 0:
                        logger.warning("Team watchdog: deadlock detected (%.0fs no progress)", since_progress)
                        await self._event_queue.put(await self._emit_team_error(
                            f"Team 检测到死锁 ({DEADLOCK_TIMEOUT}s 无进展)"
                        ))
                        self._cancelled = True
                        return

            # 循环依赖检测
            cycles = await self.task_store.check_circular_dependency()
            if cycles:
                cycle_strs = [" → ".join(c) for c in cycles]
                logger.warning("Team watchdog: circular dependency detected: %s", cycle_strs)
                await self._event_queue.put(await self._emit_team_error(
                    f"检测到任务依赖环: {'; '.join(cycle_strs)}"
                ))
                self._cancelled = True
                return

    # ------------------------------------------------------------------
    # SSE 事件构造
    # ------------------------------------------------------------------

    async def _emit_team_status(self, phase: str, message: str) -> dict[str, Any]:
        return {
            "type": "team_status",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "phase": phase,
            "content": message,
        }

    async def _emit_team_error(self, message: str) -> dict[str, Any]:
        return {
            "type": "team_error",
            "thread_id": self._thread_id,
            "project_id": self._project_id,
            "content": message,
        }

    async def _emit_task_update(self, task: TeamTask) -> dict[str, Any]:
        return {
            "type": "team_task_update",
            "thread_id": self._thread_id,
            "task": task.model_dump(),
        }

    async def _emit_member_status(self, member: TeamMemberRuntime) -> dict[str, Any]:
        return {
            "type": "member_status",
            "thread_id": self._thread_id,
            "agent_name": member.agent_name,
            "status": member.status,
            "current_task_id": member.current_task_id,
        }
