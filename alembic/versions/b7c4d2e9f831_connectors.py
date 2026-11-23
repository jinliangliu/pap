"""外部连接器：定义、租户凭据与工具归属

Revision ID: b7c4d2e9f831
Revises: e1c7a4f92b36
Create Date: 2026-11-12 13:10:00.000000

三件事：

1. `connectors`（平台级，不挂 RLS）—— 端点与认证方式收敛到一处；
2. `connector_credentials`（租户级，挂 RLS）—— 每个组织自己的外部账号；
3. 工具归位：`custom_tools.connector_id` / `mcp_registered_tools.connector_id`，
   以及 `agents.allowed_connectors` 让智能体边界可以按连接器授权。

存量行 `connector_id` 全部为 NULL，行为与升级前完全一致：
URL 自带、令牌走 `.env` 或行上的 `token`。不做数据回填——
把二十条散工具自动猜成三个连接器，猜错比不做更糟。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c4d2e9f831"
down_revision: Union[str, Sequence[str], None] = "e1c7a4f92b36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "connectors",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "kind",
            sa.Enum("http", "mcp", name="connector_kind"),
            nullable=False,
        ),
        sa.Column("base_url", sa.String(length=512), server_default="", nullable=False),
        sa.Column(
            "auth_type",
            sa.Enum("none", "bearer", "header", "basic", name="connector_auth_type"),
            server_default="none",
            nullable=False,
        ),
        sa.Column(
            "auth_header_name", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column("timeout_seconds", sa.Integer(), server_default="30", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "health_status", sa.String(length=16), server_default="unknown", nullable=False
        ),
        sa.Column("health_message", sa.Text(), server_default="", nullable=False),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("name", name="uq_connectors_name"),
    )
    op.create_index("ix_connectors_enabled", "connectors", ["enabled"], unique=False)

    op.create_table(
        "connector_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("connector_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("secret", sa.String(length=2048), nullable=False),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "connector_id", name="uq_connector_cred_tenant"
        ),
    )
    op.create_index(
        "ix_connector_credentials_tenant_id",
        "connector_credentials",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_connector_credentials_connector_id",
        "connector_credentials",
        ["connector_id"],
        unique=False,
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    # SQLite（单元测试）没有行级安全，跳过。
    if _is_postgres():
        op.execute("ALTER TABLE connector_credentials ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE connector_credentials FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS tenant_isolation_connector_credentials "
                   "ON connector_credentials")
        op.execute(
            "CREATE POLICY tenant_isolation_connector_credentials "
            "ON connector_credentials "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )

    op.add_column("custom_tools", sa.Column("connector_id", sa.Integer(), nullable=True))
    op.create_index("ix_custom_tools_connector_id", "custom_tools", ["connector_id"])

    op.add_column(
        "mcp_registered_tools", sa.Column("connector_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_mcp_registered_tools_connector_id",
        "mcp_registered_tools",
        ["connector_id"],
    )

    # None = 不额外收窄。存量行保持 NULL，边界语义不变。
    op.add_column("agents", sa.Column("allowed_connectors", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agents", "allowed_connectors")

    op.drop_index(
        "ix_mcp_registered_tools_connector_id", table_name="mcp_registered_tools"
    )
    op.drop_column("mcp_registered_tools", "connector_id")

    op.drop_index("ix_custom_tools_connector_id", table_name="custom_tools")
    op.drop_column("custom_tools", "connector_id")

    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation_connector_credentials "
            "ON connector_credentials"
        )
        op.execute("ALTER TABLE connector_credentials NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE connector_credentials DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_connector_credentials_connector_id", table_name="connector_credentials"
    )
    op.drop_index(
        "ix_connector_credentials_tenant_id", table_name="connector_credentials"
    )
    op.drop_table("connector_credentials")

    op.drop_index("ix_connectors_enabled", table_name="connectors")
    op.drop_table("connectors")

    # 枚举类型由 create_table 自动创建，drop_table 不回收。
    if _is_postgres():
        op.execute("DROP TYPE IF EXISTS connector_auth_type")
        op.execute("DROP TYPE IF EXISTS connector_kind")
