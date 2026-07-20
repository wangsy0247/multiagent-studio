"""
SQLModel 定时任务模型

调度语义（参考 hermes-agent / Claude Code）:
- next_run_at 是调度的唯一依据（UTC naive，与项目其他表一致）
- recurring 任务执行前先推进 next_run_at（at-most-once，崩溃不重跑）
- 一次性任务即 recurring=False，next_run_at 为触发时间，触发后自动禁用
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ScheduledTask(SQLModel, table=True):
    __tablename__ = "scheduled_tasks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=100)
    prompt: str  # 到点发给 Agent 的消息，需自包含（无人值守，无法澄清）

    # ── 调度 ──
    cron_expr: Optional[str] = Field(default=None, max_length=100)  # recurring 必填，5 字段
    recurring: bool = Field(default=True)
    timezone: str = Field(default="Asia/Shanghai", max_length=50)  # cron 按此时区计算
    next_run_at: Optional[datetime] = Field(default=None, index=True)  # UTC naive
    expires_at: Optional[datetime] = Field(default=None)  # 到期自动禁用，None=永不过期
    enabled: bool = Field(default=True)

    # ── 执行 ──
    mode: str = Field(default="single", max_length=20)  # "single" | "team"
    project_id: Optional[str] = Field(default=None, max_length=50)
    agent_name: Optional[str] = Field(default=None, max_length=100)
    thread_strategy: str = Field(default="new", max_length=20)  # "new" | "fixed"
    thread_id: Optional[uuid.UUID] = Field(default=None, foreign_key="threads.id")  # fixed 策略绑定

    # ── 运行状态 ──
    last_run_at: Optional[datetime] = Field(default=None)
    last_status: Optional[str] = Field(default=None, max_length=20)  # success|error|timeout|skipped|expired
    last_error: Optional[str] = Field(default=None, max_length=2000)
    created_by: str = Field(default="user", max_length=20)  # "user" 界面创建 | "agent" Agent 对话中自建
    allow_silent: bool = Field(default=False)  # 静默模式：Agent 回 [SILENT] 时不写会话、不提醒
    created_at: datetime = Field(default_factory=_utcnow_naive)
    updated_at: datetime = Field(default_factory=_utcnow_naive)


class TaskRun(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="scheduled_tasks.id", index=True)
    thread_id: Optional[uuid.UUID] = Field(default=None, foreign_key="threads.id")
    status: str = Field(default="running", max_length=20)  # running|success|error|timeout|interrupted
    started_at: datetime = Field(default_factory=_utcnow_naive)
    finished_at: Optional[datetime] = Field(default=None)
    error: Optional[str] = Field(default=None, max_length=2000)
    summary: Optional[str] = Field(default=None, max_length=500)  # AI 回复前 N 字，列表页直接展示
    seen: bool = Field(default=False, index=True)  # 用户是否已查看（未读提醒用；静默运行直接置 True）
