"""conversation context summaries

Revision ID: b3c9d7e15a42
Revises: d4e9a1c27b58
Create Date: 2026-11-13 13:10:00.000000

长对话上下文（docs/CONTEXT.md）：

1. `conversation_summaries`：append-only 的压缩产物，最新一行生效；
   保留旧行是为了复盘「这次压缩前后各多少 token」「重建后差在哪」。
2. `conversations.last_prompt_tokens`：上一轮实测的 prompt token 数。
   放业务表而不是只记在观测里——观测可以整体关掉，而「这轮该不该压缩」
   不能因为开关状态失去依据。

不裁剪 checkpoint：它是审批中断的现场，改过就对不上审批预览里的参数。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3c9d7e15a42"
down_revision: Union[str, Sequence[str], None] = "d4e9a1c27b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "conversations",
        sa.Column(
            "last_prompt_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("through_message_id", sa.Uuid(), nullable=False),
        sa.Column("through_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("turns_covered", sa.Integer(), nullable=False),
        sa.Column("messages_covered", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("summary_chars", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("rebuilt", sa.Boolean(), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_conversation_summaries_tenant_id",
        "conversation_summaries",
        ["tenant_id"],
    )
    op.create_index(
        "ix_conversation_summaries_conversation_id",
        "conversation_summaries",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_summaries_conv_created",
        "conversation_summaries",
        ["conversation_id", "created_at"],
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    op.execute("ALTER TABLE conversation_summaries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_summaries FORCE ROW LEVEL SECURITY")
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_conversation_summaries "
        "ON conversation_summaries"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_conversation_summaries "
        "ON conversation_summaries "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_conversation_summaries "
        "ON conversation_summaries"
    )
    op.execute("ALTER TABLE conversation_summaries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_summaries DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_conversation_summaries_conv_created",
        table_name="conversation_summaries",
    )
    op.drop_index(
        "ix_conversation_summaries_conversation_id",
        table_name="conversation_summaries",
    )
    op.drop_index(
        "ix_conversation_summaries_tenant_id",
        table_name="conversation_summaries",
    )
    op.drop_table("conversation_summaries")

    op.drop_column("conversations", "last_prompt_tokens")
