"""policy_snapshots_and_write_backups

Revision ID: a7e3c1b904d2
Revises: c5e2a8f4b170
Create Date: 2026-11-23 09:50:00.000000

两张表回答同一类问题：**这次动作为什么可以被做，以及做之前是什么样子。**

- `policy_snapshots`：每个 revision 一份完整的生效配置。一次工具调用记下
  它跑在哪一版上，之后按号取回，不必猜「当时那几项是不是已经改了」。
- `write_backups`：允许一次写之前，先把将被覆盖的状态原样存下来。表名叫
  backup 而不是 version：它不承诺能回滚（外部系统回滚不了），只承诺原件还在。

`policy_snapshots` **不挂 RLS**：它是进程行为（与 `platform_settings` 同源），
不是某个租户的业务数据；同一进程只有一套行为，按租户分叉会让两份快照
互相矛盾。`write_backups` 挂 RLS：备份里是租户的业务数据。

revision 用 BIGINT 且不设序列：号由写入事务里的 `max(revision)+1` 分配，
一次配置变更落一行，量级是「人按了几次开关」，不需要序列的并发能力。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7e3c1b904d2"
down_revision: Union[str, Sequence[str], None] = "c5e2a8f4b170"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "policy_snapshots",
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("revision"),
    )

    op.create_table(
        "write_backups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("run_id", sa.String(length=255), nullable=True),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_write_backups_tenant_id", "write_backups", ["tenant_id"])
    op.create_index(
        "ix_write_backups_tenant_created",
        "write_backups",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_write_backups_tenant_tool",
        "write_backups",
        ["tenant_id", "tool_name"],
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    # 备份里是业务数据的原件，漏了这道过滤等于跨租户可读。
    op.execute("ALTER TABLE write_backups ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE write_backups FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_write_backups ON write_backups")
    op.execute(
        "CREATE POLICY tenant_isolation_write_backups "
        "ON write_backups "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_write_backups ON write_backups")
    op.execute("ALTER TABLE write_backups NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE write_backups DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_write_backups_tenant_tool", table_name="write_backups")
    op.drop_index("ix_write_backups_tenant_created", table_name="write_backups")
    op.drop_index("ix_write_backups_tenant_id", table_name="write_backups")
    op.drop_table("write_backups")
    op.drop_table("policy_snapshots")
