"""添加 Agent Team 字段到 threads 表

Revision ID: 001_add_team_fields
Create Date: 2026-07-10

新增字段:
- project_id: 项目 ID（可空，用于关联项目）
- agent_name: 单 Agent 模式指定 Agent 名称（可空）
- mode: 执行模式 "single" | "team"（默认 "single"）
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "001_add_team_fields"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("project_id", sa.String(50), nullable=True))
    op.add_column("threads", sa.Column("agent_name", sa.String(100), nullable=True))
    op.add_column("threads", sa.Column("mode", sa.String(20), nullable=False, server_default="single"))
    # 为 project_id 创建索引以加速按项目查询
    op.create_index("ix_threads_project_id", "threads", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_threads_project_id", table_name="threads")
    op.drop_column("threads", "mode")
    op.drop_column("threads", "agent_name")
    op.drop_column("threads", "project_id")
