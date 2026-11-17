"""knowledge governance: feedback, eval cases, eval runs

Revision ID: b3d9f1a7c284
Revises: e7a2c4d91b30
Create Date: 2026-11-17 14:20:00.000000

知识库从「能检索」走到「可评估、可迭代」所需的三张表：

1. ``knowledge_feedback``：一条回答的点赞/踩，只追加。刻意不建外键到
   conversations —— 会话被清理（purge_conversation）时事故记录不该跟着消失。
2. ``knowledge_eval_cases``：标注集（问句 → 应命中的文档/小节）。
3. ``knowledge_eval_runs``：一次评测的结果快照，指标落库后不再变。

三张表都走租户隔离（ENABLE + FORCE RLS），策略名与其它租户表一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d9f1a7c284"
down_revision: str | Sequence[str] | None = "e7a2c4d91b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rls(table: str) -> None:
    """与全库其它租户表完全一致的三句，别在这个文件里另立一套写法。"""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON {table} "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def _drop_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "knowledge_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("answer_excerpt", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("chunk_ids", sa.JSON(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("handled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("handled_note", sa.Text(), nullable=True),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_feedback_tenant_id", "knowledge_feedback", ["tenant_id"])
    op.create_index(
        "ix_knowledge_feedback_tenant_verdict_created",
        "knowledge_feedback",
        ["tenant_id", "verdict", "created_at"],
    )
    op.create_index(
        "ix_knowledge_feedback_tenant_message",
        "knowledge_feedback",
        ["tenant_id", "message_id"],
    )
    _rls("knowledge_feedback")

    op.create_table(
        "knowledge_eval_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("expect_found", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("expect_document", sa.JSON(), nullable=False),
        sa.Column("expect_section", sa.String(length=256), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
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
    )
    op.create_index("ix_knowledge_eval_cases_tenant_id", "knowledge_eval_cases", ["tenant_id"])
    op.create_index(
        "ix_knowledge_eval_cases_tenant_enabled",
        "knowledge_eval_cases",
        ["tenant_id", "enabled"],
    )
    _rls("knowledge_eval_cases")

    op.create_table(
        "knowledge_eval_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("case_total", sa.Integer(), nullable=False),
        sa.Column("scored", sa.Integer(), nullable=False),
        sa.Column("recall", sa.Float(), nullable=False),
        sa.Column("precision", sa.Float(), nullable=False),
        sa.Column("hit_at_k", sa.Float(), nullable=False),
        sa.Column("mrr", sa.Float(), nullable=False),
        sa.Column("false_hit_rate", sa.Float(), nullable=False),
        sa.Column("zero_hit_rate", sa.Float(), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_eval_runs_tenant_id", "knowledge_eval_runs", ["tenant_id"])
    op.create_index(
        "ix_knowledge_eval_runs_tenant_created",
        "knowledge_eval_runs",
        ["tenant_id", "created_at"],
    )
    _rls("knowledge_eval_runs")


def downgrade() -> None:
    """Downgrade schema."""
    for table, indexes in (
        ("knowledge_eval_runs", ["ix_knowledge_eval_runs_tenant_created"]),
        ("knowledge_eval_cases", ["ix_knowledge_eval_cases_tenant_enabled"]),
        ("knowledge_feedback", ["ix_knowledge_feedback_tenant_verdict_created", "ix_knowledge_feedback_tenant_message"]),
    ):
        _drop_rls(table)
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_table(table)
