"""platform_settings：管理员可改、改完立刻生效的运行配置

Revision ID: a9d1e4b7c250
Revises: c5e8a1f7d3b9
Create Date: 2026-11-15 01:10:00.000000

为什么单独一张表：`.env` 改一次要重启，而且只有能登服务器的人能改。
观测开关这类「管理员在控制台决定」的项需要一个能持久化、能审计的来源。

为什么**不挂 RLS**：这些是进程行为开关，不是租户业务数据。同一个进程只有
一套行为，「A 租户开观测、B 租户不开」没有意义。与 `model_providers` 同属
平台级全局表。

迁移本身不写任何初始行：库里为空时读取端回落到 `.env` 的默认值
（`OBS_ENABLED`，现在默认 true），所以「刚部署」与「管理员从没动过」
表现一致——不会因为迁移反而改变线上行为。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9d1e4b7c250"
down_revision: Union[str, Sequence[str], None] = "c5e8a1f7d3b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("platform_settings")
