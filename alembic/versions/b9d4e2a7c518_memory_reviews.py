"""memory_reviews

Revision ID: b9d4e2a7c518
Revises: f2b6d8c4a071
Create Date: 2026-11-11 09:40:00.000000

记忆复核建议表。模型只产出建议，应用由人确认，所以「建议」与「记忆」
必须分表：建议有独立生命周期（pending / applied / dismissed），
同一条记忆会被反复复核，塞进 memories.metadata_json 会越滚越乱。

`memory_id` 带 ON DELETE CASCADE：用户物理删除一条记忆时，
指向它的建议也应一起消失，否则页面上会出现「建议改一条不存在的记忆」。

`merge_target_id` 刻意不设外键：目标被删后建议应变成「无法应用」，
而不是把建议一起级联删掉、让人不知道为什么少了一条。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9d4e2a7c518"
down_revision: Union[str, Sequence[str], None] = "f2b6d8c4a071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memory_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "correct",
                "merge",
                "retire",
                "keep",
                name="memory_review_action",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "applied",
                "dismissed",
                name="memory_review_status",
                native_enum=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_title", sa.String(length=256), nullable=True),
        sa.Column("proposed_content", sa.Text(), nullable=True),
        sa.Column("merge_target_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("model", sa.String(length=128), server_default="", nullable=False),
        sa.Column(
            "prompt_version", sa.String(length=32), server_default="", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_reviews_tenant_id", "memory_reviews", ["tenant_id"])
    op.create_index("ix_memory_reviews_user_id", "memory_reviews", ["user_id"])
    op.create_index("ix_memory_reviews_memory_id", "memory_reviews", ["memory_id"])
    op.create_index(
        "ix_memory_reviews_tenant_user_status",
        "memory_reviews",
        ["tenant_id", "user_id", "status"],
    )
    op.create_index(
        "ix_memory_reviews_tenant_memory_status",
        "memory_reviews",
        ["tenant_id", "memory_id", "status"],
    )
    op.create_index(
        "ix_memory_reviews_tenant_created",
        "memory_reviews",
        ["tenant_id", "created_at"],
    )

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    op.execute("ALTER TABLE memory_reviews ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_reviews FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_memory_reviews ON memory_reviews")
    op.execute(
        "CREATE POLICY tenant_isolation_memory_reviews ON memory_reviews "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_memory_reviews ON memory_reviews")
    op.execute("ALTER TABLE memory_reviews NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_reviews DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_memory_reviews_tenant_created", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_tenant_memory_status", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_tenant_user_status", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_memory_id", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_user_id", table_name="memory_reviews")
    op.drop_index("ix_memory_reviews_tenant_id", table_name="memory_reviews")
    op.drop_table("memory_reviews")
