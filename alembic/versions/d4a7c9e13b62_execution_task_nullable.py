# alembic migration: 让一次性执行也能落库
"""task_executions.task_id 改为可空。

P0 的沙箱直写路径不传 task_id（对话里的一次性代码执行没有所属任务），
但建表时把它设成了 NOT NULL——配了 PAP_SANDBOX_DATABASE_URL 后每次
执行都会在 INSERT 时撞非空约束，沙箱返回 503「结果未知」。

task_id 非空只对「任务执行」成立；一次性执行本来就没有任务。这里放宽
约束，让同一个 execution 表同时容纳两种来源；任务的归属由平台侧写入，
不靠这一列是否为空来判定。

枚举、RLS 与其余列不变。SQLite 测试用 Base.metadata.create_all 建表，
不读这条迁移；DDL 只走 PostgreSQL。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4a7c9e13b62"
down_revision: str | Sequence[str] | None = "c1f7b2e94a83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "task_executions",
        "task_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    # 只在所有 execution 都有 task_id 时才能收回；有一次性执行就拒绝回滚，
    # 不用「随便填一个 UUID」伪造归属。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM task_executions WHERE task_id IS NULL) THEN
                RAISE EXCEPTION
                    '无法回滚：存在没有所属任务的 execution，不能把 task_id 改回 NOT NULL';
            END IF;
        END $$;
        """
    )
    op.alter_column(
        "task_executions",
        "task_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
