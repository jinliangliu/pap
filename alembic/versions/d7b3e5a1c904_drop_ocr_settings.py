"""drop ocr_settings (OCR now served over MCP)

Revision ID: d7b3e5a1c904
Revises: c9f4a1e83b57
Create Date: 2026-11-06 12:10:00.000000

OCR 配置从平台内迁走：识别由本机 OCR 服务的 MCP 接口 `ocr_file` 提供，
平台不再保存 provider / base_url / api_key 这类设置，所以这张单例表可以删。

表里只存过运行参数，没有业务数据；真要回退，downgrade 会把表结构建回来
（内容为空，需要重新在控制台填一遍）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7b3e5a1c904"
down_revision: Union[str, Sequence[str], None] = "c9f4a1e83b57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("ocr_settings")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "ocr_settings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="openai", nullable=False),
        sa.Column("model", sa.String(length=128), server_default="gpt-4o-mini", nullable=False),
        sa.Column("api_key", sa.String(length=512), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("ocr_lang", sa.String(length=64), server_default="ch+en", nullable=False),
        sa.Column("storage", sa.String(length=32), server_default="local", nullable=False),
        sa.Column(
            "upload_dir", sa.String(length=512), server_default="./data/uploads", nullable=False
        ),
        sa.Column(
            "max_image_bytes", sa.Integer(), server_default="10485760", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
