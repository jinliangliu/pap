"""自研观测：obs_traces / obs_spans

Revision ID: c9f4a1e83b57
Revises: b8d2c5f7a140
Create Date: 2026-11-05 14:40:00.000000

LangSmith 退役后的替代：一轮对话落一条 trace，节点/模型/工具调用各落一条 span。

两张表都**逐行带 tenant_id 并挂 RLS**（与 messages 同级）。原因是 full 模式下
`obs_traces.input_text` / `obs_spans.content` 存的就是用户完整提问与模型完整回答，
敏感级别与消息表一致，隔离要求不能更低。

表建好但不填数据：开关默认关闭（OBS_ENABLED=false），部署时不产生任何写入。
上线前清数据的步骤见 docs/OBSERVABILITY.md §11.2。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f4a1e83b57"
down_revision: Union[str, Sequence[str], None] = "b8d2c5f7a140"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RLS_TABLES = ("obs_traces", "obs_spans")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "obs_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("span_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llm_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", name="uq_obs_traces_trace_id"),
    )
    op.create_index("ix_obs_traces_tenant_id", "obs_traces", ["tenant_id"])
    op.create_index("ix_obs_traces_trace_id", "obs_traces", ["trace_id"])
    op.create_index("ix_obs_traces_user_id", "obs_traces", ["user_id"])
    op.create_index("ix_obs_traces_run_id", "obs_traces", ["run_id"])
    op.create_index("ix_obs_traces_started_at", "obs_traces", ["started_at"])
    op.create_index("ix_obs_traces_status", "obs_traces", ["status"])
    # 列表页的四种过滤组合，各给一个复合索引；单列索引在上面已经覆盖单条件查询。
    op.create_index(
        "ix_obs_traces_tenant_started", "obs_traces", ["tenant_id", "started_at"]
    )
    op.create_index(
        "ix_obs_traces_status_started", "obs_traces", ["status", "started_at"]
    )
    op.create_index(
        "ix_obs_traces_intent_started", "obs_traces", ["intent", "started_at"]
    )
    op.create_index(
        "ix_obs_traces_conv_started", "obs_traces", ["conversation_id", "started_at"]
    )

    op.create_table(
        "obs_spans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("span_id", sa.String(length=16), nullable=False),
        sa.Column("parent_span_id", sa.String(length=16), nullable=True),
        sa.Column("seq", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_obs_span_trace_span"),
    )
    op.create_index("ix_obs_spans_tenant_id", "obs_spans", ["tenant_id"])
    op.create_index("ix_obs_spans_trace_id", "obs_spans", ["trace_id"])
    op.create_index("ix_obs_spans_kind", "obs_spans", ["kind"])
    op.create_index("ix_obs_spans_trace_seq", "obs_spans", ["trace_id", "seq"])
    op.create_index("ix_obs_spans_kind_started", "obs_spans", ["kind", "started_at"])

    # ---------------- PostgreSQL RLS ----------------
    # 与 messages 同级：这两张表在 full 模式下含完整对话原文。
    if _is_postgres():
        for table in RLS_TABLES:
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
    if _is_postgres():
        for table in RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_obs_spans_kind_started", table_name="obs_spans")
    op.drop_index("ix_obs_spans_trace_seq", table_name="obs_spans")
    op.drop_index("ix_obs_spans_kind", table_name="obs_spans")
    op.drop_index("ix_obs_spans_trace_id", table_name="obs_spans")
    op.drop_index("ix_obs_spans_tenant_id", table_name="obs_spans")
    op.drop_table("obs_spans")

    op.drop_index("ix_obs_traces_conv_started", table_name="obs_traces")
    op.drop_index("ix_obs_traces_intent_started", table_name="obs_traces")
    op.drop_index("ix_obs_traces_status_started", table_name="obs_traces")
    op.drop_index("ix_obs_traces_tenant_started", table_name="obs_traces")
    op.drop_index("ix_obs_traces_status", table_name="obs_traces")
    op.drop_index("ix_obs_traces_started_at", table_name="obs_traces")
    op.drop_index("ix_obs_traces_run_id", table_name="obs_traces")
    op.drop_index("ix_obs_traces_user_id", table_name="obs_traces")
    op.drop_index("ix_obs_traces_trace_id", table_name="obs_traces")
    op.drop_index("ix_obs_traces_tenant_id", table_name="obs_traces")
    op.drop_table("obs_traces")
