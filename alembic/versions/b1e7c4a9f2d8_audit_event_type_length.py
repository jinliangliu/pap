"""放宽 audit_logs.event_type 到 varchar(64)

Revision ID: b1e7c4a9f2d8
Revises: d3f1a9c2b7e4
Create Date: 2026-11-03 10:45:00.000000

`event_type` 用 `Enum(native_enum=False)`，列长由枚举里最长的成员名推断：
原来是 26（APPROVAL_EXECUTION_UNKNOWN）。新增 APPROVAL_PARAMETER_MISMATCH
有 27 个字符，写入时 PostgreSQL 直接报
`StringDataRightTruncation: value too long for type character varying(26)`。

SQLite 不校验 varchar 长度，所以本地测试全绿而线上失败——这类问题只能在
真实库上暴露。这里把长度固定到 64，并在模型里显式声明，避免以后再加成员时
重演。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1e7c4a9f2d8'
down_revision: Union[str, Sequence[str], None] = 'd3f1a9c2b7e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.String(length=26),
            type_=sa.String(length=64),
            existing_nullable=False,
        )


def downgrade() -> None:
    # 若已写入超过 26 字符的事件类型，回退会失败——这是有意的：
    # 宁可让人先处理数据，也不要静默截断审计记录。
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.String(length=64),
            type_=sa.String(length=26),
            existing_nullable=False,
        )
