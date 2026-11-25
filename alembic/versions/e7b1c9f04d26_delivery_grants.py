# alembic migration: 定时任务投递授权范围
"""schedules / tasks 各加一列 delivery_grant_json。

授权按 schedule 落（见 docs/TRANSACTION.md §8 开放项 4）：一次授权覆盖该
定时任务的未来触发，触发时把 grant 快照进 task，之后不再回读 schedule。
grant 只含「允许哪个工具、参数落在什么范围、有效期、谁授权」，不存
roles/scopes——那会把一次性授权变成可反复使用的提权凭证。每次投递都
重新解析用户当前 RBAC 权限，收窄立即生效。

两列都可空：存量 schedule 没配投递、一次性任务不投递，都是 null，
不需要回填，也不构造默认授权。SQLite 测试走 Base.metadata.create_all，
不读这条迁移；DDL 只走 PostgreSQL。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b1c9f04d26"
down_revision: str | Sequence[str] | None = "d4a7c9e13b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column("delivery_grant_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("delivery_grant_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    # 直接丢列即可：这两列是授权快照，不是唯一事实来源，原始授权在调用方。
    # 不在这里"保留一份到别处"——同一份事实存两处迟早对不上。
    op.drop_column("tasks", "delivery_grant_json")
    op.drop_column("schedules", "delivery_grant_json")
