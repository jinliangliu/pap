"""schedules table

Revision ID: c1f7b2e94a83
Revises: b9d4f2a71c60
Create Date: 2026-11-25 09:40:00.000000

补上事务规划 docs/TRANSACTION.md §3 要求的 schedules 表——上一版迁移
b9d4f2a71c60 建了五张表但遗漏了它。新开一个迁移而不是改已提交的那条，
保证已经跑过上一版的部署能平滑升级。

与其它事务表一致：ENABLE + FORCE RLS，策略读 app.tenant_id。
触发时无请求上下文，服务层必须显式构造 context 并 bind_tenant。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1f7b2e94a83"
down_revision: str | Sequence[str] | None = "b9d4f2a71c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "schedules"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("document", "code", "schedule", name="taskkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("cron", sa.String(length=128), nullable=False),
        sa.Column("timezone_offset_hours", sa.Integer(), server_default="8", nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "idempotency_window_seconds", sa.Integer(), server_default="300", nullable=False
        ),
        sa.Column("task_template_json", sa.JSON(), nullable=True),
        sa.Column("last_task_id", sa.Uuid(), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedules_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_schedules_user_id", _TABLE, ["user_id"])
    op.create_index("ix_schedules_tenant_user", _TABLE, ["tenant_id", "user_id"])
    op.create_index("ix_schedules_due", _TABLE, ["enabled", "next_fire_at"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON {_TABLE} "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_TABLE}")
    op.execute(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_table(_TABLE)
