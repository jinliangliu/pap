"""seed platform_admin role

Revision ID: f7a2b8c3d9e1
Revises: e51c2d3f4a5b
Create Date: 2026-11-04 13:15:00.000000

平台管理员角色是「角色目录」里的固定条目，不属于任何租户。
此前只靠开发模式的 `_ensure_role` 顺带建出 `dev`，`platform_admin` 从未入库，
导致 `is_platform_admin()` 恒为 False、`/api/v1/admin/*` 永远 403。

这里只补角色定义，**不**做任何授权：谁成为平台管理员必须走
`scripts/create_tenant.py --tenant platform --member <subject> --role platform_admin`，
那条路径会写 ADMIN_ACTION 审计，且授权动作可追溯。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a2b8c3d9e1'
down_revision: Union[str, Sequence[str], None] = 'e51c2d3f4a5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_NAME = "platform_admin"
ROLE_DESCRIPTION = "平台管理员：可进入管理通道，配置平台级设置与跨租户策略。"


def upgrade() -> None:
    """幂等插入平台管理员角色。"""
    bind = op.get_bind()

    exists = bind.execute(
        sa.text("SELECT 1 FROM roles WHERE name = :name"),
        {"name": ROLE_NAME},
    ).first()

    if exists is not None:
        return

    # 用数据库侧 gen_random_uuid() 生成主键：迁移不依赖 Python 侧随机源，
    # 也避免把 UUID 以字符串形式绑定到 uuid 列时的隐式转换差异。
    bind.execute(
        sa.text(
            "INSERT INTO roles (id, name, description) "
            "VALUES (gen_random_uuid(), :name, :description)"
        ),
        {"name": ROLE_NAME, "description": ROLE_DESCRIPTION},
    )


def downgrade() -> None:
    """不删角色。

    删 roles 行会经 user_roles 的 ON DELETE CASCADE 静默撤销所有平台管理员授权，
    属于不可逆的数据破坏。角色目录回滚没有意义，这里明确留空。
    """
    pass
