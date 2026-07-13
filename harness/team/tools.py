"""Team 工具集 — 仅在 mode=team 时注册, 按角色分层.

15 个工具, 分三层:
  Lead 专属 (6):  delegate_to_member, list_teammates, broadcast, shutdown_teammate,
                   approve_plan, spawn_teammate
  共享 (5):        task_create, task_list, task_update, send_message, read_inbox
  Member 专属 (4): request_plan_approval, claim_task, idle, shutdown_response

工具中 agent 身份通过 ContextVar 自动注入.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import BaseTool, tool

from harness.team.models import TeamTaskStatus

logger = logging.getLogger(__name__)

# ── 当前 Agent 上下文 ──
_current_agent: ContextVar[str] = ContextVar("current_agent", default="unknown")
_current_agent_instance: ContextVar[Any] = ContextVar("current_agent_instance", default=None)


def set_current_agent(name: str) -> None:
    _current_agent.set(name)


def get_current_agent() -> str:
    return _current_agent.get()


def set_current_agent_instance(instance: Any) -> None:
    """注入当前 TeammateAgent 实例引用 (供工具访问 agent 内部状态)."""
    _current_agent_instance.set(instance)


def get_current_agent_instance() -> Any:
    """获取当前 TeammateAgent 实例 (可能为 None)."""
    return _current_agent_instance.get()


# ── 角色工具集定义 ──
LEAD_TOOLS = {"delegate_to_member", "list_teammates", "broadcast", "shutdown_teammate", "approve_plan", "spawn_teammate"}
SHARED_TOOLS = {"task_create", "task_list", "task_update", "send_message", "read_inbox"}
MEMBER_TOOLS = {"request_plan_approval", "claim_task", "idle", "shutdown_response"}


def create_team_tools(
    task_store: Any = None,
    message_bus: Any = None,
    subagent_manager: Any = None,
    teammates: dict | None = None,
    role: str = "member",
    spawn_callback: Any = None,  # async callable(agent_name: str) -> str
) -> list[BaseTool]:
    """构建 Team 模式专用工具集, 按角色过滤.

    Args:
        role: "lead" | "member" — 决定返回哪些工具.
              lead: LEAD_TOOLS + SHARED_TOOLS (11 个)
              member: SHARED_TOOLS + MEMBER_TOOLS (9 个)
        spawn_callback: Lead 专属, 用于动态 spawn 新 teammate 的回调.
    """

    # ═════════════════════════════════════════════════════════════════
    # Lead 专属工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def delegate_to_member(
        agent_name: str,
        instruction: str,
        task_id: str,
        context: str = "",
    ) -> str:
        """委派任务给指定 Team Member Agent 执行 (Lead 专属).

        通过任务板分配: 更新 assigned_agent 字段后,
        orchestrator 的 dispatch 循环会将其分配给目标 TeammateAgent。

        Args:
            agent_name: 目标 Member Agent 名称
            instruction: 自包含的任务指令 (会追加到任务描述中)
            task_id: 关联的任务 ID
            context: 额外的上下文信息
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"
        if task.assigned_agent and task.assigned_agent != agent_name:
            return f"Error: Task '{task_id}' already assigned to '{task.assigned_agent}'"
        if task.status.value not in ("pending",):
            return f"Error: Task '{task_id}' is not pending (current: {task.status.value})"

        # 追加委派指令到任务描述
        full_desc = task.description or ""
        if context:
            full_desc += f"\n\n[上下文]\n{context}"
        full_desc += f"\n\n[委派指令]\n{instruction}"

        await task_store.update_task(
            task_id,
            assigned_agent=agent_name,
            description=full_desc,
            status="pending",
        )
        return (
            f"已委派任务 [{task_id}] 给 '{agent_name}'。\n"
            f"任务标题: {task.title}\n"
            f"委派指令: {instruction[:200]}"
        )

    @tool
    async def list_teammates() -> str:
        """查看 Team 中所有 teammate 的当前状态 (Lead 专属)."""
        if teammates is None:
            return "Teammate 列表不可用."
        if not teammates:
            return "当前 Team 中没有 teammate."
        lines = [f"共 {len(teammates)} 个 teammate:\n"]
        for name, tm in teammates.items():
            icon = {"idle": "🟢", "working": "🔵", "failed": "❌"}.get(
                tm.status.value if hasattr(tm.status, 'value') else str(tm.status), "❓")
            task_info = f" (任务: {tm.current_task_id})" if tm.current_task_id else ""
            lines.append(f"- {icon} **{name}** [{tm.status}] — 完成 {tm.completed_tasks}{task_info}")
        return "\n".join(lines)

    @tool
    async def shutdown_teammate(agent_name: str) -> str:
        """ 请求关闭指定 teammate (Lead 专属)."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        req_id = str(_uuid.uuid4())[:8]
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=agent_name,
            msg_type=TeamMessageType.SHUTDOWN_REQUEST, content="shutdown", request_id=req_id,
        )
        await message_bus.send(msg)
        return f"已向 '{agent_name}' 发送关闭请求 (req_id={req_id})"

    @tool
    async def approve_plan(request_id: str, requester: str, approve: bool, feedback: str = "") -> str:
        """审批 Teammate 提交的计划 —  结构化审批 (Lead 专属).

        收到 plan_approval_request 后, 审阅计划内容并用此工具回复:
        - approve=True: 批准计划, Teammate 将继续执行
        - approve=False: 拒绝计划, Teammate 需要调整后重新提交

        Args:
            request_id: 计划审批请求的 ID (必须与收到的请求匹配)
            requester: 提交计划的 Agent 名称
            approve: 是否批准计划
            feedback: 审批反馈 (批准时可提供补充建议, 拒绝时必须说明原因)
        """
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType

        status_text = f"approved. {feedback}" if approve else f"rejected: {feedback or '计划未通过审批'}"
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=requester,
            msg_type=TeamMessageType.PLAN_APPROVAL_RESPONSE,
            content=status_text, request_id=request_id,
        )
        await message_bus.send(msg)

        # 更新本地追踪器
        agent = get_current_agent_instance()
        if agent is not None:
            agent._pending_requests[request_id] = {
                "type": "plan_approval",
                "status": "approved" if approve else "rejected",
                "from": requester,
                "feedback": feedback,
            }

        action = "批准" if approve else "拒绝"
        return f"已{action}来自 '{requester}' 的计划 (req_id={request_id})。"

    @tool
    async def spawn_teammate(agent_name: str) -> str:
        """动态创建并启动一个新的 Teammate Agent (Lead 专属).

        当你需要扩充团队时调用此工具。新 teammate 将:
        1. 使用其预配置的 SOUL.md 作为 system prompt
        2. 自动进入 IDLE 状态, 等待任务分配
        3. 支持  自主认领任务板上的未分配任务

        Args:
            agent_name: 要创建的 Agent 名称 (必须在 agents 配置中存在)
        """
        if spawn_callback is None:
            return "Error: Spawn not available (no orchestrator callback)"
        try:
            result = await spawn_callback(agent_name)
            return result
        except Exception as exc:
            return f"Error: Failed to spawn '{agent_name}': {exc}"

    # ═════════════════════════════════════════════════════════════════
    # 共享工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def task_create(
        title: str, description: str = "", assigned_agent: str = "",
        dependencies: list[str] | None = None, priority: str = "medium",
    ) -> str:
        """在 Team 任务板上创建新任务.

        Args:
            title: 任务标题; description: 详细描述
            assigned_agent: 分配给谁 (留空=自动分配); dependencies: 依赖的任务 ID
            priority: "low"|"medium"|"high"|"critical"
        """
        if task_store is None:
            return "Error: Task store not available"
        task = await task_store.create_task(
            title=title, description=description,
            assigned_agent=assigned_agent if assigned_agent else None,
            dependencies=dependencies or [], priority=priority,
        )
        return (f"任务已创建:\n- ID: {task.id}\n- 标题: {task.title}\n"
                f"- 状态: {task.status}\n- 分配: {task.assigned_agent or '待分配'}")

    @tool
    async def task_list(status: str = "", assigned_agent: str = "") -> str:
        """查询 Team 任务板. status 过滤: pending|in_progress|completed|failed|cancelled."""
        if task_store is None:
            return "Error: Task store not available"
        status_filter = TeamTaskStatus(status) if status else None
        agent_filter = assigned_agent if assigned_agent else None
        tasks = await task_store.list_tasks(status=status_filter, assigned_agent=agent_filter)
        if not tasks:
            return "任务板为空."
        icons = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫"}
        lines = [f"共 {len(tasks)} 个任务:\n"]
        for t in tasks:
            lines.append(f"- {icons.get(t.status.value, '❓')} [{t.id}] {t.title} (分配: {t.assigned_agent or '无'})")
            if t.dependencies:
                lines.append(f"  依赖: {', '.join(t.dependencies)}")
        return "\n".join(lines)

    @tool
    async def task_update(task_id: str, status: str = "", output: str = "", assigned_agent: str = "") -> str:
        """更新任务状态. status: pending|in_progress|completed|failed|cancelled."""
        if task_store is None:
            return "Error: Task store not available"
        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"
        updates: dict[str, Any] = {}
        if status:
            try:
                updates["status"] = TeamTaskStatus(status)
            except ValueError:
                return f"Error: Invalid status '{status}'"
        if output:
            updates["output"] = output
        if assigned_agent:
            updates["assigned_agent"] = assigned_agent
        if not updates:
            return "未提供任何更新字段."
        updated = await task_store.update_task(task_id, **updates)
        if updated is None:
            return f"Error: Failed to update task '{task_id}'"
        return f"任务 [{task_id}] 已更新: {updated.title} → {updated.status.value}"

    @tool
    async def send_message(to_agent: str, content: str, task_id: str = "") -> str:
        """向 Team 中的另一个 Agent 发送消息."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=to_agent,
            msg_type=TeamMessageType.TEXT, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return f"消息已发送给 '{to_agent}'"

    @tool
    async def read_inbox() -> str:
        """读取自己的收件箱 (drain-on-read)."""
        if message_bus is None:
            return "Error: Message bus not available"
        messages = await message_bus.read_inbox(get_current_agent())
        if not messages:
            return "收件箱为空."
        lines = [f"共 {len(messages)} 条新消息:\n"]
        for msg in messages:
            lines.append(f"- [{msg.msg_type.value}] 来自 **{msg.from_agent}**: {msg.content[:200]}")
        return "\n".join(lines)

    @tool
    async def broadcast(content: str, task_id: str = "") -> str:
        """向 Team 全体成员广播消息."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=None,
            msg_type=TeamMessageType.BROADCAST, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return "广播消息已发送给全体成员。"

    # ═════════════════════════════════════════════════════════════════
    # Member 专属工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def request_plan_approval(plan_description: str) -> str:
        """ 向 Lead 请求审批高风险操作计划 (Member 专属)."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        req_id = str(_uuid.uuid4())[:8]
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=None,
            msg_type=TeamMessageType.PLAN_APPROVAL_REQUEST,
            content=plan_description, request_id=req_id,
        )
        await message_bus.send(msg)
        return f"审批请求已发送 (req_id={req_id})。等待 Lead 审批中..."

    @tool
    async def claim_task(task_id: str) -> str:
        """ 自主认领任务板上未分配的任务 (Member 专属)."""
        if task_store is None:
            return "Error: Task store not available"
        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"
        if task.assigned_agent is not None:
            return f"Error: Task '{task_id}' already assigned to '{task.assigned_agent}'"
        if task.status.value != "pending":
            return f"Error: Task '{task_id}' is not pending (current: {task.status.value})"
        await task_store.update_task(task_id, assigned_agent=get_current_agent(), status="in_progress")
        return f"已认领任务 [{task_id}]: {task.title}"

    @tool
    async def shutdown_response(request_id: str, requester: str, approve: bool, reason: str = "") -> str:
        """响应关机请求 —  结构化握手 (Member 专属).

        收到 shutdown_request 后, 由 LLM 决策是否批准关机:
        - approve=True: 批准关机, Agent 将在当前轮次结束后优雅退出
        - approve=False: 拒绝关机, 继续执行当前任务

        Args:
            request_id: 关机请求的 ID (必须与收到的请求匹配)
            requester: 发起关机请求的 Agent 名称
            approve: 是否批准关机
            reason: 拒绝原因 (approve=False 时建议提供)
        """
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType

        status_text = "approved" if approve else f"rejected: {reason}" if reason else "rejected"
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=requester,
            msg_type=TeamMessageType.SHUTDOWN_RESPONSE,
            content=status_text, request_id=request_id,
        )
        await message_bus.send(msg)

        if approve:
            agent = get_current_agent_instance()
            if agent is not None:
                agent._should_exit = True
                agent._pending_requests[request_id] = {
                    "type": "shutdown", "status": "approved", "from": requester,
                }
            return f"已批准关机请求 (req_id={request_id})。Agent 将在当前任务完成后退出。"

        # 拒绝: 更新追踪器
        agent = get_current_agent_instance()
        if agent is not None:
            agent._pending_requests[request_id] = {
                "type": "shutdown", "status": "rejected", "from": requester, "reason": reason,
            }
        return f"已拒绝关机请求 (req_id={request_id})。继续执行当前任务。"

    @tool
    async def idle() -> str:
        """ 声明当前 Agent 进入 IDLE 状态 (Member 专属)."""
        return f"Agent '{get_current_agent()}' 已进入 IDLE 状态, 等待新任务或消息。"

    # ═════════════════════════════════════════════════════════════════
    # 按角色组装
    # ═════════════════════════════════════════════════════════════════

    all_tools: dict[str, BaseTool] = {
        # Lead 专属
        "delegate_to_member": delegate_to_member,
        "list_teammates": list_teammates,
        "shutdown_teammate": shutdown_teammate,
        "approve_plan": approve_plan,
        "spawn_teammate": spawn_teammate,
        # 共享
        "task_create": task_create,
        "task_list": task_list,
        "task_update": task_update,
        "send_message": send_message,
        "read_inbox": read_inbox,
        "broadcast": broadcast,
        # Member 专属
        "request_plan_approval": request_plan_approval,
        "claim_task": claim_task,
        "shutdown_response": shutdown_response,
        "idle": idle,
    }

    if role == "lead":
        allowed = LEAD_TOOLS | SHARED_TOOLS
    else:
        allowed = SHARED_TOOLS | MEMBER_TOOLS

    result = [t for name, t in all_tools.items() if name in allowed]
    logger.info("create_team_tools: role=%s → %d tools: %s", role, len(result), sorted(allowed))
    return result
