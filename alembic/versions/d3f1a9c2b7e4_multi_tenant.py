"""multi tenant: tenants, membership scope, quotas, tool policies, artifacts, RLS

Revision ID: d3f1a9c2b7e4
Revises: b7c41f0d2a90
Create Date: 2026-11-02 16:10:00.000000

这一版把「单租户 + 全局角色」升级为「多租户 + 按租户解析角色」：

1. 新增 tenants / tenant_quotas / tenant_tool_policies / artifacts；
2. user_roles 重建为代理主键 + (tenant_id, user_id, role_id) 唯一，历史数据归入 default 租户；
3. station_grants、messages 补 tenant_id；
4. PostgreSQL 上对全部业务表启用 RLS（FORCE），策略读取 app.tenant_id。

注意：RLS 只有在应用账号不是超级用户、且没有 BYPASSRLS 时才真正生效。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f1a9c2b7e4'
down_revision: Union[str, Sequence[str], None] = 'b7c41f0d2a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RLS_TABLES = (
    "conversations",
    "messages",
    "agent_runs",
    "approval_requests",
    "audit_logs",
    "documents",
    "document_chunks",
    "memories",
    "station_grants",
    "knowledge_index_state",
    "tenant_quotas",
    "tenant_tool_policies",
    "artifacts",
)

DEFAULT_TENANT = "default"
PLATFORM_TENANT = "platform"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "suspended",
                "archived",
                name="tenant_status",
                native_enum=False,
            ),
            server_default="active",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
    )
    op.create_index("ix_tenants_status", "tenants", ["status"], unique=False)

    # 默认租户与平台租户必须存在：前者承接历史数据，后者承载平台管理员。
    for tenant_id, name in ((DEFAULT_TENANT, "默认租户"), (PLATFORM_TENANT, "平台")):
        op.execute(
            sa.text(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) "
                "SELECT :tenant_id, :name, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = :tenant_id)"
            ).bindparams(tenant_id=tenant_id, name=name)
        )

    op.create_table(
        "tenant_quotas",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("daily_chat_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls_per_minute", sa.Integer(), server_default="0", nullable=False),
        sa.Column("knowledge_max_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_quotas"),
    )

    op.create_table(
        "tenant_tool_policies",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "tool_name", name="pk_tenant_tool_policies"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("object_key", name="uq_artifacts_object_key"),
    )
    op.create_index("ix_artifacts_tenant_id", "artifacts", ["tenant_id"], unique=False)
    op.create_index("ix_artifacts_user_id", "artifacts", ["user_id"], unique=False)
    op.create_index(
        "ix_artifacts_tenant_user_created",
        "artifacts",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_artifacts_tenant_conversation",
        "artifacts",
        ["tenant_id", "conversation_id"],
        unique=False,
    )

    # ---------------- user_roles：改成代理主键 + 租户唯一 ----------------
    op.rename_table("user_roles", "user_roles_legacy")

    op.create_table(
        "user_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_user_roles"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "role_id",
            name="uq_user_role_tenant_user_role",
        ),
    )
    op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"], unique=False)
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"], unique=False)
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"], unique=False)
    op.create_index(
        "ix_user_roles_tenant_user",
        "user_roles",
        ["tenant_id", "user_id"],
        unique=False,
    )

    if _is_postgres():
        op.execute(
            "INSERT INTO user_roles (id, tenant_id, user_id, role_id) "
            "SELECT gen_random_uuid(), 'default', user_id, role_id FROM user_roles_legacy"
        )
    else:
        op.execute(
            "INSERT INTO user_roles (id, tenant_id, user_id, role_id) "
            "SELECT lower(hex(randomblob(16))), 'default', user_id, role_id "
            "FROM user_roles_legacy"
        )

    op.drop_table("user_roles_legacy")

    # ---------------- station_grants：补租户并调整唯一约束 ----------------
    with op.batch_alter_table("station_grants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=128), nullable=True))

    op.execute("UPDATE station_grants SET tenant_id = 'default' WHERE tenant_id IS NULL")

    with op.batch_alter_table("station_grants", schema=None) as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.drop_constraint("uq_station_grant_user_station", type_="unique")
        batch_op.create_unique_constraint(
            "uq_station_grant_tenant_user_station",
            ["tenant_id", "user_id", "station_id"],
        )
        batch_op.create_index("ix_station_grants_tenant_id", ["tenant_id"], unique=False)

    # ---------------- messages：补租户，历史数据跟随会话 ----------------
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=128), nullable=True))

    op.execute(
        "UPDATE messages SET tenant_id = COALESCE("
        "(SELECT c.tenant_id FROM conversations c WHERE c.id = messages.conversation_id),"
        " 'default') WHERE tenant_id IS NULL"
    )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.create_index("ix_messages_tenant_id", ["tenant_id"], unique=False)

    # ---------------- PostgreSQL RLS ----------------
    if _is_postgres():
        for table in RLS_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            # FORCE：表 owner 也受策略约束。超级用户/BYPASSRLS 仍然绕过，
            # 所以应用连接必须使用普通角色（见 docs/MULTI_TENANCY.md）。
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                "USING (tenant_id = current_setting('app.tenant_id', true)) "
                "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
            )


def downgrade() -> None:
    """Downgrade schema."""
    if _is_postgres():
        for table in RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index("ix_messages_tenant_id")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("station_grants", schema=None) as batch_op:
        batch_op.drop_index("ix_station_grants_tenant_id")
        batch_op.drop_constraint("uq_station_grant_tenant_user_station", type_="unique")
        batch_op.create_unique_constraint(
            "uq_station_grant_user_station",
            ["user_id", "station_id"],
        )
        batch_op.drop_column("tenant_id")

    # user_roles 回退：放下租户列，恢复 (user_id, role_id) 复合主键。
    op.rename_table("user_roles", "user_roles_legacy")

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles_legacy"),
    )
    op.execute(
        "INSERT INTO user_roles (user_id, role_id) "
        "SELECT DISTINCT user_id, role_id FROM user_roles_legacy"
    )
    op.drop_table("user_roles_legacy")

    op.drop_table("artifacts")
    op.drop_table("tenant_tool_policies")
    op.drop_table("tenant_quotas")
    op.drop_table("tenants")
