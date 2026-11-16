"""智能体上的委派预算：深度上限与单轮次数

Revision ID: d4e9a1c27b58
Revises: a8d3c1f62b04
Create Date: 2026-11-12 17:20:00.000000

两列都可空、无默认值、不回填（理由见 `docs/A2A.md` §8 与 `app/models/agent.py`）：

- 空 = 走平台默认（当前为 2 跳 / 3 次）。回填成 2 / 3 会把「没配」变成
  「配了一个具体数字」，日后调整默认值时存量数据不会跟着动，而界面上
  显示的是用户从没设过的值。
- 取值只让这一环更严：折进生效上限时取 min，不会凭一行配置放宽整条链。

`0` 是合法配置（禁止转交），所以不能用「0 等于没配」那种约定。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e9a1c27b58"
down_revision: Union[str, Sequence[str], None] = "a8d3c1f62b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("agents", sa.Column("max_delegation_depth", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("delegation_budget", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agents", "delegation_budget")
    op.drop_column("agents", "max_delegation_depth")
