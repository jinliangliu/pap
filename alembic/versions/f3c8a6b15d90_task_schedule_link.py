# alembic migration: 任务记录来源 schedule
"""tasks 加 schedule_id 列与 (schedule_id, status) 索引。

撤销投递授权要即时打断该 schedule 下已入队的任务，必须能精确反查它们。
不用 idempotency_key 的 `schedule:<uuid>:<stamp>` 前缀去 LIKE——那是把关联
编码进字符串，一旦格式调整就会静默漏掉在途任务。

存量行的 schedule_id 为 NULL：它们的历史授权快照不动，也不追溯撤销。
SQLite 测试走 Base.metadata.create_all，不读这条迁移；DDL 只走 PostgreSQL。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3c8a6b15d90"
down_revision: str | Sequence[str] | None = "e7b1c9f04d26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("schedule_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_tasks_schedule_status",
        "tasks",
        ["schedule_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_schedule_status", table_name="tasks")
    op.drop_column("tasks", "schedule_id")
