"""workflows and workflow_runs

Revision ID: c5e8a1f7d3b9
Revises: b3c9d7e15a42
Create Date: 2026-11-13 22:30:00.000000

声明式编排（docs/ORCHESTRATION.md）：

1. `workflows`：版本化的图。`revision` 每改一次 +1，保存时编译一次
   （`app/orchestration/definition.py`），所以库里存的一定是能跑的图。
2. `workflow_runs`：一次执行的现场。`cursor` 是「下一个要跑的节点」，
   **在调用之前**写入；`blackboard` 是节点结论；两者合起来就是断点。

为什么不复用 `agent_runs`：那一行的语义是「用户发了一句话，这一轮跑了什么」。
编排一次会调用多个 Agent，但它只是那一轮里的一件事——两件事记在同一张表上，
「这个会话跑了几轮」与「这个流程跑了几次」会互相污染。

`workflows` 不存「谁能触发它」：触发权的判定仍然走 `agent_subset` 与人的 scope，
图里连了不等于有权连（ORCHESTRATION.md §2 第一条）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5e8a1f7d3b9"
down_revision: Union[str, Sequence[str], None] = "b3c9d7e15a42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_workflows_tenant_name"),
    )
    op.create_index("ix_workflows_tenant_id", "workflows", ["tenant_id"])
    op.create_index("ix_workflows_name", "workflows", ["name"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_name", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "PAUSED",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                name="workflowrunstatus",
            ),
            nullable=False,
        ),
        sa.Column("cursor", sa.String(length=32), nullable=True),
        sa.Column("blackboard", sa.JSON(), nullable=False),
        sa.Column("nodes_json", sa.JSON(), nullable=False),
        sa.Column("nodes_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_by", sa.String(length=128), nullable=True),
        sa.Column("root_agent", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_runs_tenant_id", "workflow_runs", ["tenant_id"])
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
    op.create_index("ix_workflow_runs_workflow_name", "workflow_runs", ["workflow_name"])
    op.create_index("ix_workflow_runs_thread_id", "workflow_runs", ["thread_id"])
    op.create_index("ix_workflow_runs_conversation_id", "workflow_runs", ["conversation_id"])
    op.create_index("ix_workflow_runs_run_id", "workflow_runs", ["run_id"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_approval_id", "workflow_runs", ["approval_id"])
    op.create_index(
        "ix_workflow_runs_tenant_started", "workflow_runs", ["tenant_id", "started_at"]
    )
    op.create_index(
        "ix_workflow_runs_status_started", "workflow_runs", ["status", "started_at"]
    )

    for table in ("workflows", "workflow_runs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("workflows", "workflow_runs"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for name in (
        "ix_workflow_runs_status_started",
        "ix_workflow_runs_tenant_started",
        "ix_workflow_runs_approval_id",
        "ix_workflow_runs_status",
        "ix_workflow_runs_run_id",
        "ix_workflow_runs_conversation_id",
        "ix_workflow_runs_thread_id",
        "ix_workflow_runs_workflow_name",
        "ix_workflow_runs_workflow_id",
        "ix_workflow_runs_tenant_id",
    ):
        op.drop_index(name, table_name="workflow_runs")
    op.drop_table("workflow_runs")

    op.drop_index("ix_workflows_name", table_name="workflows")
    op.drop_index("ix_workflows_tenant_id", table_name="workflows")
    op.drop_table("workflows")

    sa.Enum(name="workflowrunstatus").drop(op.get_bind(), checkfirst=True)
