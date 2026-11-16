# app/models/approval.py
"""写操作审批记录。

核心安全属性：
- parameter_hash 把批准绑定到精确参数，参数变化必须重新预览；
- expires_at 防止陈旧预览被延后执行；
- idempotency_key 防止网络重试造成重复写入；
- execution_result / verification 区分：
  已提交、已执行、已核验、失败、结果未知。
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
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class RiskLevel(str, enum.Enum):
    READ = "read"
    NORMAL = "normal"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


class ApprovalStatus(str, enum.Enum):
    """审批/执行状态。

    PENDING           已生成预览，等待用户确认
    APPROVED          用户已确认，尚未开始外部执行
    REJECTED          用户拒绝
    EXPIRED           超过有效期
    EXECUTING         正在调用外部系统
    EXECUTED          外部调用已返回，但未核验
    VERIFIED          执行完成且通过外部回读核验
    FAILED            明确未执行或外部系统明确返回失败
    RECOVERY_REQUIRED 执行过程异常，需要按幂等键回读恢复
    EXECUTION_UNKNOWN 无法确认外部系统是否已执行，禁止自动重发
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"


TERMINAL_APPROVAL_STATUSES = frozenset(
    {
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.VERIFIED,
        ApprovalStatus.FAILED,
        ApprovalStatus.EXECUTION_UNKNOWN,
        ApprovalStatus.RECOVERY_REQUIRED,
    }
)


class ApprovalRequest(TenantScoped, Base):
    __tablename__ = "approval_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[str] = mapped_column(String(255), index=True)

    # 发起这张审批单的智能体，以及**审批时**的职责边界快照。
    #
    # 快照是必要的：执行发生在第二次请求里，中间可能过了几分钟，
    # Agent 的边界随时可能被改。执行时取「快照 ∩ 当前」——
    # 收窄立即生效，放宽不生效。只存 agent_id 做不到这一点：
    # 期间把只读 Agent 放开成可写，已按只读批准的写操作就会照新边界执行。
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_allowed_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    agent_can_write: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # 委派链：这张单子是「谁转交给谁」的产物，按跳序从根到直接上级。
    # 形如 `[{"id": "...", "name": "cs"}, {"id": "...", "name": "ops"}]`，
    # 执行这张单子的 Agent 是 `agent_id`，**不在链上**。
    #
    # 为什么要存整条链而不是只存「上一级」：批准与执行跨两次请求，
    # 期间任何一跳被收窄，这张单子就成了「一次不该发生的委派的产物」。
    # 只复核直接上级会漏掉根节点被收窄的情况（A 收窄 → A→B→C 仍然放行）。
    #
    # 存 id 而不是只存名字：名字在 Agent 删除后可被新 Agent 占用，
    # 而复核必须问「还是当初那一跳吗」。名字仍然存一份，是为了删除后仍可读
    # （与 `agent_delegated` 审计同一口径）。
    delegation_chain: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

    # 发起方所在的会话线程。委派发生在子线程里（子图有自己的 thread_id），
    # 而结论要回到用户看得见的那个会话——所以两个线程都得留着。
    # 非委派单为空，此时 thread_id 就是会话线程。
    parent_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 这张单子开在哪一次「运行」里（`obs_traces.operation_id`）。
    # 确认后是新的 trace，只有从这里继承，前半段（发起）与后半段（执行）
    # 才在运行视图里是同一次运行。观测关闭时为空，此时不显示运行视图。
    # 与 `A2A.md` §10 拒绝的 `parent_trace_id` 不同：那是 trace 之间的树边，
    # 这是分组键。
    operation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    tool_display_name: Mapped[str] = mapped_column(String(255))
    risk_level: Mapped[RiskLevel] = mapped_column(Enum(RiskLevel))

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus),
        default=ApprovalStatus.PENDING,
        index=True,
    )

    # 用户请求的最小审计摘要，不存完整对话。
    reason: Mapped[str] = mapped_column(Text)

    parameters_json: Mapped[dict] = mapped_column(JSON)
    parameter_hash: Mapped[str] = mapped_column(String(64), index=True)
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # 每条写操作独立生成；重试必须复用同一 key。
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
    )

    # 外部系统返回的任务/命令标识，用于恢复时回读。
    external_operation_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )
    recovery_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    execution_attempts: Mapped[int] = mapped_column(Integer, default=0)

    execution_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_approval_status_expiry", "status", "expires_at"),
    )

    # ---------------- 委派语义的派生字段 ----------------

    @property
    def delegated_from(self) -> str | None:
        """直接上级的 name；非委派单为 None。

        派生而不是单存一列：链里已经有一份，多存一份就多一处可能对不上，
        而审批卡上的一句话与复核用的判断必须来自同一个来源。
        """
        chain = self.delegation_chain or []
        if not chain:
            return None
        head = chain[-1]
        return str(head.get("name") or "") or None

    @property
    def delegation_depth(self) -> int:
        """第几跳。0 表示不是委派来的。"""
        return len(self.delegation_chain or [])

    @property
    def is_delegated(self) -> bool:
        return bool(self.delegation_chain)
