"""定时任务静默模式与未读标记

Revision ID: 004_silent_and_seen
Create Date: 2026-07-19

- scheduled_tasks.allow_silent: 静默模式开关（Agent 回 [SILENT] 时不写会话、不提醒）
- task_runs.seen: 用户是否已查看（未读提醒用）；存量记录直接标记为已读
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "004_silent_and_seen"
down_revision: Union[str, None] = "003_task_created_by"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("allow_silent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "task_runs",
        sa.Column("seen", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_task_runs_seen", "task_runs", ["seen"])
    # 存量执行记录视为已读，避免迁移后突然出现一堆"未读"
    op.execute("UPDATE task_runs SET seen = true")


def downgrade() -> None:
    op.drop_index("ix_task_runs_seen", table_name="task_runs")
    op.drop_column("task_runs", "seen")
    op.drop_column("scheduled_tasks", "allow_silent")
