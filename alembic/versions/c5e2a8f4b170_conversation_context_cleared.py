"""conversation_context_cleared

Revision ID: c5e2a8f4b170
Revises: b3d9f1a7c284
Create Date: 2026-11-20 10:30:00.000000

`/clear` 的落点：会话上一个「上下文清空水位」。

为什么是水位而不是删消息：聊天窗口里的历史是审计记录，删掉之后
「他当时到底问过什么」就再也答不出来。而模型这一轮该看到什么，
只需要一条时间线水位就能表达。

两列都允许 NULL：没有 NULL 就无法区分「从没清过」与「在某个时刻清过」，
而前者必须保持「全部历史都算」。存量会话全部为 NULL，正是它们该有的语义。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5e2a8f4b170"
down_revision: Union[str, Sequence[str], None] = "b3d9f1a7c284"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "conversations",
        sa.Column("context_cleared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("context_cleared_through_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("conversations", "context_cleared_through_id")
    op.drop_column("conversations", "context_cleared_at")
