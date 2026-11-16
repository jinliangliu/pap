# app/models/workflow.py
"""编排（Workflow）：版本化的图，以及它的执行记录。

两张表的分工与 `agents` / `agent_runs` 完全同构，理由也一样：

- `workflows` 是**配置**：图在保存时编译一次，`revision` 每改一次 +1；
- `workflow_runs` 是**事实**：跑起来的那一刻把 `definition` 的修订号冻在行上。

为什么必须冻结修订号：管理员在流程跑到第三个节点时改图，改的是下一次的行为。
已发的 run 若跟着变，`cursor` 会指向一个在新图里已经改名的节点——那种故障只在
有人恰好边跑边改时出现，查起来毫无线索。

`blackboard` / `nodes_json` 都是 JSON 列：黑板的形状由引擎定义
（`app/orchestration/engine.py`），这里存的是它的快照，不在这里做结构约束。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class WorkflowRunStatus(str, enum.Enum):
    """一次执行的终态集合。

    `PAUSED` 是**未完结**：它停在某个节点的审批上，人点完之后从 `cursor` 继续。
    把它写成 `COMPLETED` 会让「这个流程跑过几次」多算一遍，而「卡在哪」永远查不到。
    """

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# 还没走到结局的状态：维护任务只扫这些。
OPEN_RUN_STATUSES = frozenset({WorkflowRunStatus.RUNNING, WorkflowRunStatus.PAUSED})


class Workflow(TenantScoped, Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # 租户内唯一，且创建后不可改：它出现在 `/workflow <name>` 与审计里，
    # 改名会让历史记录指向一个再也找不到的入口。
    name: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 每次改定义 +1。run 记的是这个数，不是 `updated_at`：
    # 时间戳能回答「什么时候改的」，回答不了「跑的是哪一版」。
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    definition: Mapped[dict] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_workflows_tenant_name"),
    )


class WorkflowRun(TenantScoped, Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # 流程被删之后 run 仍然要能读：`workflow_name` 与 `revision` 是冻结的副本。
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workflow_name: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 跑起来那一刻的图。冻结一份副本而不是只记 revision：
    # 管理员在流程跑到第三个节点时改图，改的是下一次；已发的 run 必须按旧图走完，
    # 否则 `cursor` 会指向一个在新图里已经改名的节点。几 KB 换「同一条 run
    # 前后两半用同一张图」，很划算。
    definition: Mapped[dict] = mapped_column(JSON, default=dict)

    # 发起方的会话与运行。运行视图靠 `run_id` 把「编排」与「那一轮对话」对上，
    # 靠 `conversation_id` 把结论写回用户看得见的那个会话。
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus),
        default=WorkflowRunStatus.RUNNING,
        index=True,
    )

    # 下一个要跑的节点。**在调用之前**落库：崩在中间的 run 因此知道自己停在哪。
    cursor: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 黑板与节点流水。前者是引擎续跑的输入，后者是运行页要展示的东西。
    blackboard: Mapped[dict] = mapped_column(JSON, default=dict)
    nodes_json: Mapped[list] = mapped_column(JSON, default=list)
    nodes_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 停在审批上时，等的是哪一张单子。审批终态后由收口路径读它来唤醒。
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # 给用户的那句结论（最后跑完的节点的输出）。单独存一份而不是每次从黑板算：
    # 流程被改过之后，黑板里的节点 id 可能已经对不上了，而这句话仍然要说得出。
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 发起这次 run 的智能体名。续跑要靠它把链路补全：审批可能出现在第一个节点，
    # 那时 `nodes_json` 里一个已跑的 Agent 都没有，重放不出「谁发起的」。
    root_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_workflow_runs_tenant_started", "tenant_id", "started_at"),
        Index("ix_workflow_runs_status_started", "status", "started_at"),
    )
