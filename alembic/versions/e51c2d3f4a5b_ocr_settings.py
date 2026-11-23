"""ocr service settings

Revision ID: e51c2d3f4a5b
Revises: d3f1a9c2b7e4
Create Date: 2026-11-04 10:00:00.000000

平台级 OCR / 视觉服务配置（单例行）。该表为全局配置，不参与租户隔离，
不挂 RLS 策略。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e51c2d3f4a5b'
down_revision: Union[str, Sequence[str], None] = 'b1e7c4a9f2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
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
        sa.Column("upload_dir", sa.String(length=512), server_default="./data/uploads", nullable=False),
        sa.Column(
            "max_image_bytes",
            sa.Integer(),
            server_default="10485760",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("ocr_settings")
