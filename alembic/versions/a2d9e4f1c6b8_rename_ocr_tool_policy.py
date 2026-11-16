"""rename ocr_image tool policy to ocr_document

Revision ID: a2d9e4f1c6b8
Revises: c4a8e1d7f2b9
Create Date: 2026-11-05 10:00:00.000000

工具从 `ocr_image` 改名为 `ocr_document`（加入 PDF 支持）。

`tenant_tool_policies` 以 (tenant_id, tool_name) 为主键，工具名是**持久化的业务标识**：
改名后旧行会变成孤儿，而新名字查不到策略时按 `ToolPolicy()` 默认值处理——
默认是启用。也就是说，凡是显式**禁用**过 ocr_image 的租户，升级后会静默地重新获得该能力。
这类「越升级越宽松」的偏差不会有任何报错，只能靠迁移把策略搬过去。

搬行用 INSERT ... SELECT 而不是「查出来再插」：
`allowed_roles` 的类型随方言不同（PG 上是数组，SQLite 上是文本），
取回 Python 再写回会引入适配问题，纯 SQL 交给数据库自己保持类型。

**必须用管理账号执行迁移**：`tenant_tool_policies` 开了 forced RLS，
没有 `app.tenant_id` 上下文时 INSERT/DELETE 会影响 0 行且不报错——
迁移会「成功」，策略却原样留着。`/root/.pap-admin-db-url` 是 bypassrls 角色，
`deploy/sync.sh --migrate` 用的就是它。

幂等：目标行已存在时不覆盖（以管理员升级后手工设置的值优先）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2d9e4f1c6b8"
down_revision: Union[str, Sequence[str], None] = "c4a8e1d7f2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "ocr_image"
_NEW = "ocr_document"


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "INSERT INTO tenant_tool_policies "
            "(tenant_id, tool_name, enabled, allowed_roles, credential_ref) "
            "SELECT p.tenant_id, CAST(:new AS VARCHAR(128)), p.enabled, "
            "p.allowed_roles, p.credential_ref "
            "FROM tenant_tool_policies p "
            "WHERE p.tool_name = :old "
            "AND NOT EXISTS (SELECT 1 FROM tenant_tool_policies q "
            "                WHERE q.tenant_id = p.tenant_id AND q.tool_name = :new)"
        ),
        {"old": _OLD, "new": _NEW},
    )

    # 旧名已不再注册，留着只会让人以为它还在生效。
    conn.execute(
        sa.text("DELETE FROM tenant_tool_policies WHERE tool_name = :name"),
        {"name": _OLD},
    )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "INSERT INTO tenant_tool_policies "
            "(tenant_id, tool_name, enabled, allowed_roles, credential_ref) "
            "SELECT p.tenant_id, CAST(:old AS VARCHAR(128)), p.enabled, "
            "p.allowed_roles, p.credential_ref "
            "FROM tenant_tool_policies p "
            "WHERE p.tool_name = :new "
            "AND NOT EXISTS (SELECT 1 FROM tenant_tool_policies q "
            "                WHERE q.tenant_id = p.tenant_id AND q.tool_name = :old)"
        ),
        {"old": _OLD, "new": _NEW},
    )
    conn.execute(
        sa.text("DELETE FROM tenant_tool_policies WHERE tool_name = :name"),
        {"name": _NEW},
    )
