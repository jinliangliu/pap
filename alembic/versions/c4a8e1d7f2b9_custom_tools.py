"""custom http tools

Revision ID: c4a8e1d7f2b9
Revises: b3d5f7a9c1e2
Create Date: 2026-11-04 16:00:00.000000

管理员在控制台创建的自定义 HTTP Tool。平台级表，不挂 RLS：
工具注册表全局唯一，跨租户可见性由 tenant_tool_policies 再过滤一层。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c4a8e1d7f2b9"
down_revision: Union[str, Sequence[str], None] = "b3d5f7a9c1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # risklevel 枚举由 approvals 表在初始迁移里创建，这里复用，不重复建类型。
    risk_level = postgresql.ENUM(
        "READ",
        "NORMAL",
        "WRITE",
        "EXECUTE",
        "DESTRUCTIVE",
        "EXTERNAL",
        name="risklevel",
        create_type=False,
    )

    op.create_table(
        "custom_tools",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("method", sa.String(length=8), server_default="GET", nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("risk_level", risk_level, server_default="READ", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="20", nullable=False),
        sa.Column("params", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("headers", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.UniqueConstraint("name", name="uq_custom_tools_name"),
    )
    op.create_index("ix_custom_tools_enabled", "custom_tools", ["enabled"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_custom_tools_enabled", table_name="custom_tools")
    op.drop_table("custom_tools")
