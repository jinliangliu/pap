"""agents

Revision ID: a2f8c4d60b73
Revises: e7c3b5d91f26
Create Date: 2026-11-11 11:55:00.000000

智能体（Agent）成为一等公民：有名字、有职责、有边界、有生命周期。

**刻意不在迁移里回填默认 Agent。** 存量租户的默认 Agent 由服务层在第一次
需要时惰性创建（`agent_service.ensure_default`）。理由：迁移里写一份
description / instruction 等于把当时的提示词钉死在历史里，以后想统一
调整所有租户的默认职责，只能再写一次数据迁移，而且没人知道哪些是「历史遗留」。

`is_default` 用部分唯一索引兜住「每租户至多一个默认」：不传 agent 的请求
全落到它身上，出现两个就意味着「不传时行为不确定」。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2f8c4d60b73"
down_revision: Union[str, Sequence[str], None] = "e7c3b5d91f26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("allowed_skills", sa.JSON(), nullable=True),
        sa.Column(
            "can_write",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "disabled", name="agent_status"),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"], unique=False)
    op.create_index(
        "ix_agents_tenant_status", "agents", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "uq_agents_tenant_default",
        "agents",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    op.execute("ALTER TABLE agents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agents FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_agents ON agents")
    op.execute(
        "CREATE POLICY tenant_isolation_agents "
        "ON agents "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_agents ON agents")
    op.execute("ALTER TABLE agents NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agents DISABLE ROW LEVEL SECURITY")

    op.drop_index("uq_agents_tenant_default", table_name="agents")
    op.drop_index("ix_agents_tenant_status", table_name="agents")
    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_table("agents")
    # 枚举类型由 create_table 自动创建，drop_table 不会回收它，显式清理，
    # 否则重复 upgrade/downgrade 会在第二次 upgrade 时报「type already exists」。
    op.execute("DROP TYPE IF EXISTS agent_status")
