"""远端 MCP Tool 接入表

Revision ID: e3a71c5b48d2
Revises: a2d9e4f1c6b8
Create Date: 2026-11-05 11:00:00.000000

平台级表，不挂 RLS：MCP 通道本身只有一条（地址与令牌在 .env），
工具注册表也全局唯一。跨租户可见性照旧由 tenant_tool_policies 再过滤。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e3a71c5b48d2"
down_revision: Union[str, Sequence[str], None] = "a2d9e4f1c6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # risklevel 枚举由 approvals 表在初始迁移里创建，复用，不重复建类型。
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
        "mcp_registered_tools",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("local_name", sa.String(length=64), nullable=False),
        sa.Column("remote_name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("risk_level", risk_level, server_default="READ", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="60", nullable=False),
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
        sa.UniqueConstraint("local_name", name="uq_mcp_registered_tools_local_name"),
    )
    op.create_index(
        "ix_mcp_registered_tools_enabled", "mcp_registered_tools", ["enabled"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_mcp_registered_tools_enabled", table_name="mcp_registered_tools")
    op.drop_table("mcp_registered_tools")
