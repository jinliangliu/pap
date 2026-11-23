"""approval_requests：委派链与发起会话线程

Revision ID: c2f7a8e41d63
Revises: b7c4d2e9f831
Create Date: 2026-11-12 15:05:00.000000

P2（写操作委派）之前，子调用一律只读，审批单只可能由「用户直接对话的那个
Agent」开出，所以单子上只需要一个 `agent_*` 边界快照。

允许被委派方执行写操作之后，同一张单子多了一层语义：它是**某一跳委派的产物**。
执行时要复核的不再只是「这个 Agent 现在还能不能做」，还有「当初那一跳现在
还该不该发生」——批准与执行跨两次请求，期间上游被收窄，这张单子就作废。

两列：

- `delegation_chain`：从根到直接上级的 `[{id, name}]`，空/None = 非委派。
  存整条链而不是只存上一级，是因为 A 收窄时 A→B→C 这一条也必须一起作废；
  存 id 而不是只存名字，是因为名字在删除后可被新 Agent 占用。
- `parent_thread_id`：发起方所在的会话线程。委派跑在子线程里，结论要回到
  用户看得见的那个会话——两个线程都得留着。

不给存量行回填：存量单子都不是委派来的，`NULL` 就是它们的真实状态，
复核逻辑对 `NULL` 直接跳过，行为与改造前逐字一致。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2f7a8e41d63"
down_revision: Union[str, Sequence[str], None] = "b7c4d2e9f831"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "approval_requests",
        sa.Column("delegation_chain", sa.JSON(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("parent_thread_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approval_requests", "parent_thread_id")
    op.drop_column("approval_requests", "delegation_chain")
