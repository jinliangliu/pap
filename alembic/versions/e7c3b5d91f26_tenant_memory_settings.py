"""tenant_memory_settings

Revision ID: e7c3b5d91f26
Revises: b9d4e2a7c518
Create Date: 2026-11-11 10:05:00.000000

租户级记忆配置。**没有行 = 全部沿用平台默认**，所以这张表长期可以是空的，
不需要为存量租户回填——回填会把当前默认值固化成「明确设置」，
以后平台调整默认值时这些租户就悄悄不跟随了。

`dream_enabled` 刻意允许 NULL：三态才能区分「没配过」与「明确关掉」。
`dream_days` / `dream_max_per_run` 用 0 表示沿用平台默认，与 tenant_quotas 同约定。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7c3b5d91f26"
down_revision: Union[str, Sequence[str], None] = "b9d4e2a7c518"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenant_memory_settings",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("dream_enabled", sa.Boolean(), nullable=True),
        sa.Column("dream_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "dream_max_per_run", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    op.execute("ALTER TABLE tenant_memory_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_memory_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_tenant_memory_settings "
        "ON tenant_memory_settings"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_tenant_memory_settings "
        "ON tenant_memory_settings "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_tenant_memory_settings "
        "ON tenant_memory_settings"
    )
    op.execute("ALTER TABLE tenant_memory_settings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_memory_settings DISABLE ROW LEVEL SECURITY")

    op.drop_table("tenant_memory_settings")
