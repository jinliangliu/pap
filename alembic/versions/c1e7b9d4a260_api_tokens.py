"""api_tokens：个人访问令牌，供外部宿主以真实用户身份调用

Revision ID: c1e7b9d4a260
Revises: a9d1e4b7c250
Create Date: 2026-11-16 01:05:00.000000

只存令牌的 sha256，不存明文；明文只在签发那一次返回给用户。

**刻意不挂 RLS**：鉴权发生在知道租户之前——请求里只有一枚令牌，
服务端得先按哈希找到行才知道它属于谁。挂上 `tenant_id = app.tenant_id`
的策略之后，这条查询永远返回空，表现是「令牌签出来了但怎么都用不了」。
与 `users` / `roles` 同属身份平面，读它的每一处自己带 `user_id` 条件。

不给存量数据补任何令牌：令牌只能由用户自己签发，
迁移里凭空造一枚等于在库里留一把没人认领的钥匙。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e7b9d4a260"
down_revision: Union[str, Sequence[str], None] = "a9d1e4b7c250"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # 哈希唯一：同一枚令牌不可能对应两行，也顺手挡住重复写入。
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )
    op.create_index("ix_api_tokens_tenant_id", "api_tokens", ["tenant_id"])
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_user_created", "api_tokens", ["user_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_api_tokens_user_created", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_tenant_id", table_name="api_tokens")
    op.drop_table("api_tokens")
