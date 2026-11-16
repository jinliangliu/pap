"""obs_traces.agent_name

Revision ID: b7e5a1c93d08
Revises: a2f8c4d60b73
Create Date: 2026-11-11 12:05:00.000000

观测需要回答「哪个智能体在替用户做事」。放进 `attrs`（JSON）也能存，
但按 Agent 聚合调用量 / 错误率是运营的基本动作，值得一个可索引的列。

可空、无默认值：存量 trace 本来就没有这个信息，用空串回填等于伪造
「当时用的是某个叫空字符串的 Agent」。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e5a1c93d08"
down_revision: Union[str, Sequence[str], None] = "a2f8c4d60b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "obs_traces",
        sa.Column("agent_name", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("obs_traces", "agent_name")
