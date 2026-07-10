"""Team 工具集 — 仅在 mode=team 时注册。

8 个工具:
- delegate_to_member: Lead 委派任务给 member
- task_create: 在任务板上创建任务
- task_list: 查询任务板
- task_update: 更新任务状态
- send_message: 发送点对点/广播消息
- broadcast: 广播消息给全体成员
- review_task: 审阅已完成任务
- merge_result: 合并 worktree 结果
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from harness.team.models import TeamTaskStatus

logger = logging.getLogger(__name__)


def create_team_tools(
    task_store: Any = None,
    message_bus: Any = None,
    subagent_manager: Any = None,
) -> list[BaseTool]:
    """构建 Team 模式专用工具集。

    仅在 mode=team 时调用。单 Agent 模式下这些工具不可用。
    """

    # ── delegate_to_member ───────────────────────────────────────────

    @tool
    async def delegate_to_member(
        agent_name: str,
        instruction: str,
        task_id: str,
        context: str = "",
        require_review: bool = False,
    ) -> str:
        """委派任务给指定 Team Member Agent 执行。

        Member Agent 会使用自己的 SOUL.md 人格、工具组和记忆来执行任务。
        执行完成后会自动更新任务状态。

        Args:
            agent_name: 目标 Member Agent 名称（必须是项目成员）
            instruction: 自包含的任务指令（5 要素：目标/背景/范围/约束/格式）
            task_id: 关联的任务 ID
            context: 额外的上下文信息
            require_review: 是否需要在完成后由 Lead 审阅
        """
        if subagent_manager is None:
            return "Error: SubAgent manager not initialized"

        if task_store is not None:
            task = await task_store.get_task(task_id)
            if task is None:
                return f"Error: Task '{task_id}' not found"
            if task.assigned_agent and task.assigned_agent != agent_name:
                return (
                    f"Error: Task '{task_id}' is already assigned to "
                    f"'{task.assigned_agent}'"
                )

        # 通过 SubagentManager 执行
        full_instruction = instruction
        if context:
            full_instruction = f"[上下文]\n{context}\n\n[任务]\n{instruction}"

        try:
            result = await subagent_manager.execute(
                agent_name, full_instruction,
            )
            output = result.output or ""
            if result.error:
                output = f"[{result.status}] {result.error}\n{output}"
            return output
        except Exception as exc:
            return f"Error delegating to '{agent_name}': {exc}"

    # ── task_create ──────────────────────────────────────────────────

    @tool
    async def task_create(
        title: str,
        description: str = "",
        assigned_agent: str = "",
        dependencies: list[str] | None = None,
        priority: str = "medium",
    ) -> str:
        """在 Team 任务板上创建新任务。

        Args:
            title: 任务标题（简洁描述）
            description: 详细描述（包含目标、范围、约束、输出格式）
            assigned_agent: 分配给哪个 Member（留空则由调度器自动分配）
            dependencies: 依赖的任务 ID 列表（这些任务必须先完成）
            priority: 优先级 — "low" | "medium" | "high" | "critical"
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.create_task(
            title=title,
            description=description,
            assigned_agent=assigned_agent if assigned_agent else None,
            dependencies=dependencies or [],
            priority=priority,
        )
        return (
            f"任务已创建:\n"
            f"- ID: {task.id}\n"
            f"- 标题: {task.title}\n"
            f"- 状态: {task.status}\n"
            f"- 分配: {task.assigned_agent or '待分配'}\n"
            f"- 依赖: {task.dependencies or '无'}"
        )

    # ── task_list ────────────────────────────────────────────────────

    @tool
    async def task_list(
        status: str = "",
        assigned_agent: str = "",
    ) -> str:
        """查询 Team 任务板上的任务列表。

        Args:
            status: 按状态过滤 — "pending"|"in_progress"|"reviewing"|"completed"|"failed"|"blocked"
            assigned_agent: 按分配的 Agent 名称过滤
        """
        if task_store is None:
            return "Error: Task store not available"

        status_filter = TeamTaskStatus(status) if status else None
        agent_filter = assigned_agent if assigned_agent else None
        tasks = await task_store.list_tasks(
            status=status_filter, assigned_agent=agent_filter,
        )

        if not tasks:
            return "任务板为空。"

        lines = [f"共 {len(tasks)} 个任务:\n"]
        for t in tasks:
            status_icon = {
                TeamTaskStatus.PENDING: "⏳",
                TeamTaskStatus.ASSIGNED: "📋",
                TeamTaskStatus.IN_PROGRESS: "🔄",
                TeamTaskStatus.REVIEWING: "👀",
                TeamTaskStatus.COMPLETED: "✅",
                TeamTaskStatus.FAILED: "❌",
                TeamTaskStatus.BLOCKED: "🚫",
            }.get(t.status, "❓")
            lines.append(
                f"- {status_icon} [{t.id}] {t.title} "
                f"(分配: {t.assigned_agent or '无'}, 优先级: {t.priority})"
            )
            if t.dependencies:
                lines.append(f"  依赖: {', '.join(t.dependencies)}")
        return "\n".join(lines)

    # ── task_update ──────────────────────────────────────────────────

    @tool
    async def task_update(
        task_id: str,
        status: str = "",
        output: str = "",
        assigned_agent: str = "",
    ) -> str:
        """更新任务状态。

        Args:
            task_id: 任务 ID
            status: 新状态 — "pending"|"in_progress"|"reviewing"|"completed"|"failed"|"blocked"
            output: 任务输出/结果摘要
            assigned_agent: 重新分配给其他 Agent
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"

        updates: dict[str, Any] = {}
        if status:
            try:
                new_status = TeamTaskStatus(status)
                updates["status"] = new_status
            except ValueError:
                return f"Error: Invalid status '{status}'"
        if output:
            updates["output"] = output
        if assigned_agent:
            updates["assigned_agent"] = assigned_agent

        if not updates:
            return "未提供任何更新字段。"

        updated = await task_store.update_task(task_id, **updates)
        if updated is None:
            return f"Error: Failed to update task '{task_id}'"

        return (
            f"任务 [{task_id}] 已更新:\n"
            f"- 标题: {updated.title}\n"
            f"- 状态: {updated.status}\n"
            f"- 分配: {updated.assigned_agent or '无'}"
        )

    # ── send_message ─────────────────────────────────────────────────

    @tool
    async def send_message(
        to_agent: str,
        content: str,
        task_id: str = "",
    ) -> str:
        """向 Team 中的另一个 Agent 发送消息。

        Args:
            to_agent: 接收方 Agent 名称
            content: 消息内容
            task_id: 关联的任务 ID（可选）
        """
        if message_bus is None:
            return "Error: Message bus not available"

        from harness.team.models import TeamMessage, TeamMessageType

        msg = TeamMessage(
            from_agent="system",  # 由调用方在运行时填充
            to_agent=to_agent,
            msg_type=TeamMessageType.TEXT,
            content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return f"消息已发送给 '{to_agent}'"

    # ── broadcast ────────────────────────────────────────────────────

    @tool
    async def broadcast(
        content: str,
        task_id: str = "",
    ) -> str:
        """向 Team 全体成员广播消息。

        Args:
            content: 广播内容
            task_id: 关联的任务 ID（可选）
        """
        if message_bus is None:
            return "Error: Message bus not available"

        from harness.team.models import TeamMessage, TeamMessageType

        msg = TeamMessage(
            from_agent="system",
            to_agent=None,  # None = broadcast
            msg_type=TeamMessageType.BROADCAST,
            content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return "广播消息已发送给全体成员。"

    # ── review_task ──────────────────────────────────────────────────

    @tool
    async def review_task(
        task_id: str,
        approved: bool,
        feedback: str = "",
    ) -> str:
        """审阅已完成的任务。

        Args:
            task_id: 要审阅的任务 ID
            approved: True=通过（标记 completed）, False=打回（标记 rejected/pending）
            feedback: 审阅意见
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"

        if task.status != TeamTaskStatus.REVIEWING:
            return f"Error: Task '{task_id}' is not in reviewing status (current: {task.status})"

        if approved:
            await task_store.update_task(
                task_id,
                status=TeamTaskStatus.COMPLETED,
                output=(task.output or "") + f"\n[审阅通过: {feedback}]" if feedback else "",
            )
            return f"任务 [{task_id}] 审阅通过 ✅"
        else:
            await task_store.update_task(
                task_id,
                status=TeamTaskStatus.PENDING,
                output=(task.output or "") + f"\n[审阅打回: {feedback}]" if feedback else "",
            )
            return f"任务 [{task_id}] 已打回重做 🔄\n意见: {feedback}"

    # ── merge_result ─────────────────────────────────────────────────

    @tool
    async def merge_result(
        source_agents: list[str],
        strategy: str = "sequential",
    ) -> str:
        """合并多个 Member Agent 的 worktree 结果到主 workspace。

        Args:
            source_agents: 要合并的 Agent 名称列表
            strategy: 合并策略 — "sequential"（顺序合并）| "llm_review"（LLM 裁决冲突）

        Returns:
            合并结果摘要
        """
        if subagent_manager is None:
            return "Error: SubAgent manager not available"

        # TODO: 集成 GitWorktreeManager 进行实际文件合并
        # 当前版本仅提供基本骨架
        return (
            f"合并请求已提交:\n"
            f"- 来源: {', '.join(source_agents)}\n"
            f"- 策略: {strategy}\n"
            f"- 状态: 合并功能将在后续版本中完善"
        )

    # ── 组装返回 ─────────────────────────────────────────────────────

    return [
        delegate_to_member,
        task_create,
        task_list,
        task_update,
        send_message,
        broadcast,
        review_task,
        merge_result,
    ]
