"""transaction tables

Revision ID: b9d4f2a71c60
Revises: a7e3c1b904d2
Create Date: 2026-11-24 16:40:00.000000

事务层的五张表（workspaces / tasks / task_executions / execution_traces /
execution_states）与三项租户配额，见 docs/TRANSACTION.md §3。

两个刻意的设计：

- 枚举统一按 **value** 落库（小写），与 ORM 的 `values_callable` 一致。
  agent_status 那次「成员名 vs 小写 value」的线上 500 不再重演。
- 执行轨迹与当前态分开存：trace 是追加写的事实日志，state 是当前快照。
  审批回读要能比对两者，而不是只信执行那一次响应。

所有表与既有租户表一致：ENABLE + FORCE ROW LEVEL SECURITY，策略读
`app.tenant_id`。即使日后执行记录改由沙箱服务端直写，写入方也必须
显式设置这个变量——RLS 不因为「写入方是可信服务」就网开一面。

`cpu_ms` / `duration_ms` 允许 NULL：沙箱老版本不回报这些字段时，
NULL 表示「没测到」，0 表示「测到了且没消耗」，计量上不能混。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9d4f2a71c60"
down_revision: str | Sequence[str] | None = "a7e3c1b904d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TENANT_TABLES = (
    "workspaces",
    "tasks",
    "task_executions",
    "execution_traces",
    "execution_states",
)


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("root_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active", "idle", "archived", "deleted",
                name="workspacestatus", native_enum=False,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("quota_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_workspaces_tenant_user"),
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])
    op.create_index("ix_workspaces_user_id", "workspaces", ["user_id"])
    op.create_index("ix_workspaces_tenant_status", "workspaces", ["tenant_id", "status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("document", "code", "schedule", name="taskkind", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "running", "awaiting_approval", "awaiting_input",
                "succeeded", "failed", "cancelled", "unknown",
                name="taskstatus", native_enum=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cpu_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_idempotency"),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    op.create_index("ix_tasks_workspace", "tasks", ["workspace_id"])
    op.create_index("ix_tasks_tenant_user_status", "tasks", ["tenant_id", "user_id", "status"])

    op.create_table(
        "task_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "running", "succeeded", "failed",
                "timeout", "cancelled", "unknown",
                name="executionstatus", native_enum=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("cpu_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("stdout_object_key", sa.String(length=512), nullable=True),
        sa.Column("stderr_object_key", sa.String(length=512), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "execution_id", name="uq_executions_tenant_id"),
    )
    op.create_index("ix_task_executions_tenant_id", "task_executions", ["tenant_id"])
    op.create_index("ix_task_executions_user_id", "task_executions", ["user_id"])
    op.create_index("ix_task_executions_task", "task_executions", ["task_id", "created_at"])
    op.create_index("ix_task_executions_tenant_status", "task_executions", ["tenant_id", "status"])

    op.create_table(
        "execution_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "execution_id", "seq", name="uq_trace_execution_seq"),
    )
    op.create_index("ix_execution_traces_tenant_id", "execution_traces", ["tenant_id"])
    op.create_index("ix_execution_traces_execution_seq", "execution_traces", ["execution_id", "seq"])

    op.create_table(
        "execution_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "execution_id", name="uq_state_execution"),
    )
    op.create_index("ix_execution_states_tenant_id", "execution_states", ["tenant_id"])

    with op.batch_alter_table("tenant_quotas") as batch_op:
        batch_op.add_column(sa.Column("max_concurrent_tasks", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("daily_cpu_seconds", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("workspace_max_bytes", sa.BigInteger(), server_default="0", nullable=False))

    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    with op.batch_alter_table("tenant_quotas") as batch_op:
        batch_op.drop_column("workspace_max_bytes")
        batch_op.drop_column("daily_cpu_seconds")
        batch_op.drop_column("max_concurrent_tasks")

    op.drop_table("execution_states")
    op.drop_table("execution_traces")
    op.drop_table("task_executions")
    op.drop_table("tasks")
    op.drop_table("workspaces")
