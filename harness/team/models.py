"""Agent Team 数据模型 — 任务、消息、成员运行时状态。

所有模型仅用于 Team 模式（mode=team）。单 Agent 模式不受影响。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TeamTaskStatus(str, Enum):
    """Team 任务状态 — 极简状态机，映射 A2A 标准 Task 生命周期.

    pending → in_progress → completed / failed / cancelled.
    A2A 映射: submitted=pending, working=in_progress, completed=completed,
              failed=failed, canceled=cancelled.
    """

    PENDING = "pending"            # 等待依赖完成或待分配 (A2A: submitted)
    IN_PROGRESS = "in_progress"    # member 正在执行 (A2A: working)
    COMPLETED = "completed"        # 已完成 (A2A: completed)
    FAILED = "failed"              # 执行失败 (A2A: failed)
    CANCELLED = "cancelled"        # 已取消 (A2A: canceled)

    @property
    def is_terminal(self) -> bool:
        """返回 True 表示任务已到达终态."""
        return self in {
            TeamTaskStatus.COMPLETED,
            TeamTaskStatus.FAILED,
            TeamTaskStatus.CANCELLED,
        }


class TeamTask(BaseModel):
    """Team 任务板上的一个任务。

    任务由 Lead Agent（或用户）创建，分配给 member Agent 执行。
    支持依赖关系：dependencies 中的任务必须先完成。
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    project_id: str
    title: str
    description: str = ""
    status: TeamTaskStatus = TeamTaskStatus.PENDING
    assigned_agent: str | None = None       # 被分配的 member agent name
    dependencies: list[str] = Field(default_factory=list)  # 依赖的任务 ID 列表
    priority: str = "medium"                # "low" | "medium" | "high" | "critical"
    output: str = ""                        # 执行结果文本
    error: str | None = None                # 失败原因
    retry_count: int = 0                    # 已重试次数
    max_retries: int = 3                    # 最大重试次数
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None

    def model_post_init(self, __context: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class TeamMessageType(str, Enum):
    """Team 消息类型 — 包含  结构化协议消息."""

    TEXT = "text"                              # 普通文本消息
    BROADCAST = "broadcast"                    # 广播消息
    LIFECYCLE = "lifecycle"                    # Agent 上下线通知
    # ──  结构化协议消息 ──
    SHUTDOWN_REQUEST = "shutdown_request"      # Lead → Teammate: 请求关闭
    SHUTDOWN_RESPONSE = "shutdown_response"    # Teammate → Lead: 确认/拒绝关闭
    PLAN_APPROVAL_REQUEST = "plan_approval_request"   # Teammate → Lead: 请求审批
    PLAN_APPROVAL_RESPONSE = "plan_approval_response" # Lead → Teammate: 审批结果


class TeamMessage(BaseModel):
    """Team 内 Agent 间消息 — 支持  结构化协议."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str
    to_agent: str | None = None        # None = 广播给所有人
    msg_type: TeamMessageType = TeamMessageType.TEXT
    content: str
    task_id: str | None = None         # 关联的任务 ID
    request_id: str | None = None      #  关联请求和响应 (shutdown/plan approval)
    created_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


class TeammateStatus(str, Enum):
    """Teammate Agent 生命周期状态 

    SPAWNING → WORKING ↔ IDLE → SHUTTING_DOWN → SHUTDOWN.
    """

    SPAWNING = "spawning"            # 正在初始化
    WORKING = "working"              # 正在执行任务
    IDLE = "idle"                    # 空闲，等待唤醒 ( 可自主认领)
    SHUTTING_DOWN = "shutting_down"  # 正在优雅关闭 ( shutdown handshake)
    SHUTDOWN = "shutdown"            # 已关闭 (终态)
    FAILED = "failed"                # 异常终止


class TeamMemberRuntime(BaseModel):
    """Team 成员运行时状态."""

    agent_name: str
    role: Literal["lead", "member"] = "member"
    status: TeammateStatus = TeammateStatus.SPAWNING
    current_task_id: str | None = None     # 当前正在执行的任务
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    last_error: str | None = None
    last_heartbeat: str | None = None


class RequestStatus(str, Enum):
    """协议请求状态 — FSM: pending → approved / rejected.

    用于关机协议和计划审批协议的请求追踪。
    """

    PENDING = "pending"      # 等待审批
    APPROVED = "approved"    # 已批准
    REJECTED = "rejected"    # 已拒绝


class TeamExecutionMode(str, Enum):
    """Team 执行模式."""

    LEAD_DRIVEN = "lead_driven"    # Lead Agent 拆任务 + 自动分配
    USER_DRIVEN = "user_driven"    # 用户手动创建/分配任务
    HYBRID = "hybrid"              # 用户可在 Lead 调度中介入
