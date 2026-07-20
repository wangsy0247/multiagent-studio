"""scheduled_tasks 增加 created_by 字段

Revision ID: 003_task_created_by
Create Date: 2026-07-19

标识任务来源: "user" = 用户在界面创建 | "agent" = Agent 在对话中自建
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "003_task_created_by"
down_revision: Union[str, None] = "002_scheduled_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("created_by", sa.String(20), nullable=False, server_default="user"),
    )


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "created_by")
