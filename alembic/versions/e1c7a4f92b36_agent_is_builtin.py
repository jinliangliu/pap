"""agents.is_builtin：区分系统内置智能体

Revision ID: e1c7a4f92b36
Revises: c4a9f2e75d16
Create Date: 2026-11-11 15:50:00.000000

构建助手（`studio`）要能改别人的 Agent，它自己就不能被改：
- 允许改它的 `allowed_tools`，它可以先给自己加上一个写工具，再在下一轮用上，
  两轮之间没有任何人批准过这个新增能力；
- 允许停用它，等于把控制台里唯一的自然语言管理入口关掉，而错误提示会写成
  「智能体不存在」，看起来像配置丢了。

用布尔列而不是「按 name 判断」：name 是用户可以占用的（保留名只是应用层约束），
而 `is_builtin` 是数据层事实，不会因为某次改名或重新播种而漂移。

默认 `false`，不给存量行回填：存量行全都是用户自建，`false` 就是它们的真实状态。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1c7a4f92b36"
down_revision: Union[str, Sequence[str], None] = "c4a9f2e75d16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agents",
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # 每租户最多一个内置 Agent。它与默认 Agent 同一处境：惰性创建，
    # 并发时出现两行就意味着控制台有两个构建助手。
    op.create_index(
        "uq_agents_tenant_builtin",
        "agents",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_builtin"),
        sqlite_where=sa.text("is_builtin = 1"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_agents_tenant_builtin", table_name="agents")
    op.drop_column("agents", "is_builtin")
