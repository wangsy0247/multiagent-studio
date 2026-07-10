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
    """Team 任务状态 — 从创建到合并的完整生命周期."""

    PENDING = "pending"            # 等待依赖完成或待分配
    ASSIGNED = "assigned"          # 已分配给 member，等待开始
    IN_PROGRESS = "in_progress"    # member 正在执行
    REVIEWING = "reviewing"        # Lead 正在审阅
    MERGING = "merging"            # 正在合并 worktree 结果
    COMPLETED = "completed"        # 已完成
    FAILED = "failed"              # 执行失败
    BLOCKED = "blocked"            # 被阻塞（依赖未满足或其他原因）

    @property
    def is_terminal(self) -> bool:
        """返回 True 表示任务已到达终态."""
        return self in {
            TeamTaskStatus.COMPLETED,
            TeamTaskStatus.FAILED,
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
    """Team 消息类型."""

    TEXT = "text"                # 普通文本消息
    TASK_UPDATE = "task_update"  # 任务状态变更通知
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    BROADCAST = "broadcast"      # 广播消息
    LIFECYCLE = "lifecycle"      # Agent 上下线通知


class TeamMessage(BaseModel):
    """Team 内 Agent 间消息."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str
    to_agent: str | None = None        # None = 广播给所有人
    msg_type: TeamMessageType = TeamMessageType.TEXT
    content: str
    task_id: str | None = None         # 关联的任务 ID
    created_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


class TeamMemberRuntime(BaseModel):
    """Team 成员运行时状态."""

    agent_name: str
    role: Literal["lead", "member"] = "member"
    status: Literal["idle", "busy", "done", "failed"] = "idle"
    current_task_id: str | None = None     # 当前正在执行的任务
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    last_error: str | None = None
    last_heartbeat: str | None = None


class TeamExecutionMode(str, Enum):
    """Team 执行模式."""

    LEAD_DRIVEN = "lead_driven"    # Lead Agent 拆任务 + 自动分配
    USER_DRIVEN = "user_driven"    # 用户手动创建/分配任务
    HYBRID = "hybrid"              # 用户可在 Lead 调度中介入
