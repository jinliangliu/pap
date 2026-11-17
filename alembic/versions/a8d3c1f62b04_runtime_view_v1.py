"""运行可视化 V1：span 的智能体归属 + 运行分组键 + 审批单记下所属运行

Revision ID: a8d3c1f62b04
Revises: c2f7a8e41d63
Create Date: 2026-11-12 16:10:00.000000

三列，都是「多个 Agent 一起干活」时才需要的维度（见 docs/RUNTIME_VIEW.md §4）：

- `obs_spans.agent_name`：这个 span 属于哪个智能体。委派时子图里的节点归
  被委派方。此前只有外层 `agent.ops` 这个 span 的 attrs 里带名字，它下面的
  `node.plan_request` / `llm.call` / `tool.call` 一律认不出归属。
- `obs_traces.operation_id`：一次运行的分组键。委派写操作在 `awaiting_approval`
  处结束 trace，用户几小时后确认是**新 trace**，两条此前毫无关联。
  这不是 `A2A.md` §10 拒绝的 `parent_trace_id`——那是树边，会诱发 rollup 并
  让 `by_agent` 之和不再等于 `traces`；这里是扁平分组键，同组取同值（含第一轮），
  没有任何父子关系，求和口径不受影响（有测试钉住）。
- `approval_requests.operation_id`：审批单记下自己属于哪一次运行。恢复时新的
  trace 从这里继承，否则它只能从自己的 `trace_id` 重新起一次运行，与前半段断成两条。

三列全部可空、不回填：存量 span 的归属本来就是未知，存量 trace 本来就是
一次运行的全部（`operation_id` 留空时按 `trace_id` 解释），存量审批单也没有
可继承的运行。回填等于编造一条当时并不存在的关系。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8d3c1f62b04"
down_revision: Union[str, Sequence[str], None] = "c2f7a8e41d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "obs_spans",
        sa.Column("agent_name", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_obs_spans_agent_started",
        "obs_spans",
        ["agent_name", "started_at"],
    )

    op.add_column(
        "obs_traces",
        sa.Column("operation_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_obs_traces_operation",
        "obs_traces",
        ["operation_id", "started_at"],
    )

    op.add_column(
        "approval_requests",
        sa.Column("operation_id", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approval_requests", "operation_id")
    op.drop_index("ix_obs_traces_operation", table_name="obs_traces")
    op.drop_column("obs_traces", "operation_id")
    op.drop_index("ix_obs_spans_agent_started", table_name="obs_spans")
    op.drop_column("obs_spans", "agent_name")
