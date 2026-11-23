"""skills

Revision ID: a4c8e2f6b135
Revises: d7b3e5a1c904
Create Date: 2026-11-06 12:55:00.000000

技能表：租户级，挂 RLS。`allowed_tools` 允许为 NULL，
与空数组语义不同（NULL = 不收窄，[] = 一个工具都不给），
所以这一列不能加 server_default。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a4c8e2f6b135"
down_revision: Union[str, Sequence[str], None] = "d7b3e5a1c904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=256), server_default="", nullable=False),
        sa.Column("instruction", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "trigger_keywords",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),
    )
    op.create_index("ix_skills_tenant_id", "skills", ["tenant_id"])
    op.create_index("ix_skills_tenant_enabled", "skills", ["tenant_id", "enabled"])

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    # 应用连接必须是普通角色（见 docs/MULTI_TENANCY.md）。
    op.execute("ALTER TABLE skills ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skills FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_skills ON skills")
    op.execute(
        "CREATE POLICY tenant_isolation_skills ON skills "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_skills ON skills")
    op.execute("ALTER TABLE skills NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skills DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_skills_tenant_enabled", table_name="skills")
    op.drop_index("ix_skills_tenant_id", table_name="skills")
    op.drop_table("skills")
