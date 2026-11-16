"""MCP 接入项自带端点与令牌

Revision ID: b8d2c5f7a140
Revises: e3a71c5b48d2
Create Date: 2026-11-05 12:55:00.000000

v1 假设平台只有一条 MCP 通道，地址与令牌都来自 .env。接入第二个服务
（例如本机的 OCR MCP）时，发现照着 A 地址做、接入却拿 .env 的 B 地址校验，
报出来是「远端没有这个工具」——工具存在，只是查错了服务。

两列都可空：存量行保持 NULL，行为与之前完全一致（沿用 MCP_SERVER_URL）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d2c5f7a140"
down_revision: Union[str, Sequence[str], None] = "e3a71c5b48d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "mcp_registered_tools",
        sa.Column("server_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "mcp_registered_tools",
        sa.Column("token", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("mcp_registered_tools", "token")
    op.drop_column("mcp_registered_tools", "server_url")
