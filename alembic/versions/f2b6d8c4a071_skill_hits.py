"""skill_hits + obs_traces.skill_name

Revision ID: f2b6d8c4a071
Revises: a4c8e2f6b135
Create Date: 2026-11-06 13:40:00.000000

两件事：

1. `skill_hits`：命中记录，只追加。不挂 `obs_spans` 是因为观测有保留期、
   还能被 OBS_ENABLED 整体关掉，命中趋势不该跟着消失。
2. `obs_traces.skill_name`：按技能筛列表要扫这一列，扫 JSON 的代价太大
   （同 tool_name 的处理）。

`skill_hits.skill_id` 带 ON DELETE CASCADE：删掉技能后指向空行的计数没有意义。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f2b6d8c4a071"
down_revision: Union[str, Sequence[str], None] = "a4c8e2f6b135"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skill_hits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_name", sa.String(length=64), nullable=False),
        sa.Column("matched_by", sa.String(length=16), server_default="", nullable=False),
        sa.Column("keyword", sa.String(length=64), nullable=True),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_hits_tenant_id", "skill_hits", ["tenant_id"])
    op.create_index(
        "ix_skill_hits_skill_created", "skill_hits", ["tenant_id", "skill_id", "created_at"]
    )
    op.create_index("ix_skill_hits_tenant_created", "skill_hits", ["tenant_id", "created_at"])

    # 与其它租户表一致：ENABLE + FORCE，策略读 app.tenant_id。
    op.execute("ALTER TABLE skill_hits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_hits FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_skill_hits ON skill_hits")
    op.execute(
        "CREATE POLICY tenant_isolation_skill_hits ON skill_hits "
        "USING (tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
    )

    op.add_column("obs_traces", sa.Column("skill_name", sa.String(length=64), nullable=True))
    op.create_index("ix_obs_traces_skill_started", "obs_traces", ["skill_name", "started_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_obs_traces_skill_started", table_name="obs_traces")
    op.drop_column("obs_traces", "skill_name")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_skill_hits ON skill_hits")
    op.execute("ALTER TABLE skill_hits NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_hits DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_skill_hits_tenant_created", table_name="skill_hits")
    op.drop_index("ix_skill_hits_skill_created", table_name="skill_hits")
    op.drop_index("ix_skill_hits_tenant_id", table_name="skill_hits")
    op.drop_table("skill_hits")
