"""approval_requests.agent_scope

Revision ID: c4a9f2e75d16
Revises: b7e5a1c93d08
Create Date: 2026-11-11 12:20:00.000000

审批单要回答两个问题：这是**哪个智能体**发起的、它当时**被允许做什么**。

只记 `agent_id` 不够：审批与执行跨两次请求，中间 Agent 的边界可能被改。
执行时按「审批时快照 ∩ 当前边界」判定——收窄立即生效，放宽不生效。
没有快照就只能按当前边界执行，等于「先按只读批准，后被放开成可写，写操作照跑」。

全部可空：存量审批单没有智能体信息，用空串回填会伪造
「当时用的是某个叫空字符串的 Agent」。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a9f2e75d16"
down_revision: Union[str, Sequence[str], None] = "b7e5a1c93d08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "approval_requests", sa.Column("agent_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "approval_requests",
        sa.Column("agent_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "approval_requests", sa.Column("agent_revision", sa.Integer(), nullable=True)
    )
    op.add_column(
        "approval_requests", sa.Column("agent_allowed_tools", sa.JSON(), nullable=True)
    )
    op.add_column(
        "approval_requests", sa.Column("agent_can_write", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approval_requests", "agent_can_write")
    op.drop_column("approval_requests", "agent_allowed_tools")
    op.drop_column("approval_requests", "agent_revision")
    op.drop_column("approval_requests", "agent_name")
    op.drop_column("approval_requests", "agent_id")
