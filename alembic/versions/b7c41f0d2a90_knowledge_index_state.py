"""knowledge index state

知识索引版本号：入库时自增，各进程据此判断是否需要重建内存中的检索结构。
没有这张表时只能用「切片数 + 最近写入时间」推断变化，同一秒内重复入库会漏判。

Revision ID: b7c41f0d2a90
Revises: 54522e16c913
Create Date: 2026-11-02 14:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c41f0d2a90"
down_revision: Union[str, Sequence[str], None] = "54522e16c913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "knowledge_index_state",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "version",
            sa.BigInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("knowledge_index_state")
