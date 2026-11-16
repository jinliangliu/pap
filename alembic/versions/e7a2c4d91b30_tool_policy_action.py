"""tool policy action + risk override

Revision ID: e7a2c4d91b30
Revises: c1e7b9d4a260
Create Date: 2026-11-16 12:20:00.000000

把 `tenant_tool_policies.enabled` 换成三态 `action`。

为什么不是「加一列 action 同时留着 enabled」：两者是同一事实的两种写法
（`action != "deny"` 就是 `enabled`），两份并存时总有一处先被改，
而这类不一致只在「界面上点允许、执行时却被拒」时才暴露。

回填规则：`enabled=true` → `allow`，`enabled=false` → `deny`。
**不回填成 NULL**：NULL 的语义是「未配置、跟随平台默认动作」，
把历史行改成 NULL 会让「管理员明确允许过的工具」在默认动作切成拒绝时集体失效。

`risk_override` 是新增列，全部为 NULL（沿用工具注册时的风险等级）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a2c4d91b30"
down_revision: Union[str, Sequence[str], None] = "c1e7b9d4a260"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_tool_policies",
        sa.Column("action", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "tenant_tool_policies",
        sa.Column("risk_override", sa.String(length=8), nullable=True),
    )

    # 先回填再删旧列：先删会让这一步无据可依，只能重跑迁移。
    op.execute(
        "UPDATE tenant_tool_policies SET action = CASE "
        "WHEN enabled THEN 'allow' ELSE 'deny' END"
    )

    op.drop_column("tenant_tool_policies", "enabled")


def downgrade() -> None:
    op.add_column(
        "tenant_tool_policies",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # `confirm` 在旧模型里没有对应物。回滚到布尔开关后它只能是「开放」，
    # 审批要求会随之消失——这是回滚的代价，不是遗漏，故在这里写明。
    op.execute(
        "UPDATE tenant_tool_policies SET enabled = CASE "
        "WHEN action = 'deny' THEN false ELSE true END"
    )
    op.drop_column("tenant_tool_policies", "risk_override")
    op.drop_column("tenant_tool_policies", "action")
