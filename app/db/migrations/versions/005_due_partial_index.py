"""调度查询改用部分索引

Revision ID: 005_due_partial_index
Create Date: 2026-07-19

到期任务扫描的条件是 enabled AND next_run_at <= now()。
把 ix_scheduled_tasks_next_run_at（全表索引）替换为
WHERE enabled 的部分索引：索引更小、扫描更快，且禁用任务不再进入索引。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "005_due_partial_index"
down_revision: Union[str, None] = "004_silent_and_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_scheduled_tasks_next_run_at", table_name="scheduled_tasks")
    op.create_index(
        "ix_scheduled_tasks_due",
        "scheduled_tasks",
        ["next_run_at"],
        postgresql_where=sa.text("enabled"),
        sqlite_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_due", table_name="scheduled_tasks")
    op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])
