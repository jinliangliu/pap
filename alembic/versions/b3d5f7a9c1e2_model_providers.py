"""model_providers：可配置的模型供应商

Revision ID: b3d5f7a9c1e2
Revises: f7a2b8c3d9e1
Create Date: 2026-11-04

平台级全局配置表，不挂 RLS：模型凭据由平台管理员统一维护，所有租户共用。
建表后不写入任何数据——没有供应商时运行时回落到 .env，行为与升级前一致。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d5f7a9c1e2"
down_revision: Union[str, Sequence[str], None] = "f7a2b8c3d9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False, server_default="openai-compatible"),
        sa.Column("api_key", sa.String(length=512), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("models", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("default_text_model", sa.String(length=128), nullable=True),
        sa.Column("default_image_model", sa.String(length=128), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reasoning_mode", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_model_providers_name"),
    )

    # 部分索引：最多只允许一行 is_active=true，从数据库层面堵住并发激活
    # 导致「两个都在用」的情况，不能只靠应用层先清后设。
    # 只在 PostgreSQL 上建：SQLite 不支持 WHERE 子句，退化成「整列唯一」，
    # 那会让第二行备用配置连 false 都存不进去。
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_model_providers_single_active",
            "model_providers",
            ["is_active"],
            unique=True,
            postgresql_where=sa.text("is_active"),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("uq_model_providers_single_active", table_name="model_providers")
    op.drop_table("model_providers")
