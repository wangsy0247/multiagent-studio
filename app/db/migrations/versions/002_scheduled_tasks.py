"""新增定时任务表

Revision ID: 002_scheduled_tasks
Create Date: 2026-07-19

新增表:
- scheduled_tasks: 定时任务定义（next_run_at 为调度唯一依据，执行前 CAS 推进）
- task_runs: 定时任务执行历史
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "002_scheduled_tasks"
down_revision: Union[str, None] = "001_add_team_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron_expr", sa.String(100), nullable=True),
        sa.Column("recurring", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("mode", sa.String(20), nullable=False, server_default="single"),
        sa.Column("project_id", sa.String(50), nullable=True),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("thread_strategy", sa.String(20), nullable=False, server_default="new"),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id"), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(20), nullable=True),
        sa.Column("last_error", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_scheduled_tasks_user_id", "scheduled_tasks", ["user_id"])
    op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])

    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("scheduled_tasks.id"), nullable=False),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(2000), nullable=True),
        sa.Column("summary", sa.String(500), nullable=True),
    )
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_task_id", table_name="task_runs")
    op.drop_table("task_runs")
    op.drop_index("ix_scheduled_tasks_next_run_at", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_user_id", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
