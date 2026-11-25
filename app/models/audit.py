# app/models/audit.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class AuditEventType(str, enum.Enum):
    """系统内可审计的重要事件类别。"""

    CHAT_RECEIVED = "chat_received"
    CHAT_COMPLETED = "chat_completed"

    KNOWLEDGE_INGESTED = "knowledge_ingested"
    KNOWLEDGE_DELETED = "knowledge_deleted"
    KNOWLEDGE_SEARCHED = "knowledge_searched"

    TOOL_REQUESTED = "tool_requested"
    # 「这次调用被允许了」——一条**决策**记录，写在执行之前。
    #
    # 与 TOOL_SUCCEEDED 分开，是因为它们回答的不是同一个问题：这条答
    # 「谁允许了、依据哪一版策略」，那条答「跑完没有」。只有后者时，
    # 进程在 handler 里被杀掉就什么都不剩——而「批过、开始跑、然后没了」
    # 恰恰是最需要事后看见的一种情况。
    TOOL_ALLOWED = "tool_allowed"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_DENIED = "tool_denied"

    APPROVAL_CREATED = "approval_created"
    APPROVAL_APPROVED = "approval_approved"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_EXECUTED = "approval_executed"
    APPROVAL_FAILED = "approval_failed"
    # 确认时提交的参数与预览不一致：拒绝执行并留痕（只记键名，不记值）。
    APPROVAL_PARAMETER_MISMATCH = "approval_parameter_mismatch"

    MEMORY_CREATED = "memory_created"
    MEMORY_UPDATED = "memory_updated"
    MEMORY_DELETED = "memory_deleted"

    APPROVAL_VERIFIED = "approval_verified"
    APPROVAL_EXECUTION_UNKNOWN = "approval_execution_unknown"
    APPROVAL_RECOVERY_REQUIRED = "approval_recovery_required"

    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"

    ARTIFACT_UPLOADED = "artifact_uploaded"
    ARTIFACT_DOWNLOADED = "artifact_downloaded"
    # 与 UPLOADED 分开：一个是用户传上来的材料，一个是 Agent 自己生成的结论。
    # 混成一个事件类型，事后就分不清「这份 PDF 是谁放进来的」。
    ARTIFACT_CREATED = "artifact_created"

    # 智能体的定义变了（职责 / 工具边界 / 启停），是治理事件，必须能按类型筛。
    AGENT_CREATED = "agent_created"
    AGENT_UPDATED = "agent_updated"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    AGENT_DELETED = "agent_deleted"
    # 一次委派：谁把哪件事交给了谁。资源是**接收方**（删除后仍按生前 UUID 反查）。
    # 与 trace 分开记账：trace 有保留期，这条要长期能回答「它被谁叫去干过活」。
    AGENT_DELEGATED = "agent_delegated"

    # 外部连接器：定义是平台级配置，凭据是租户级动作，分开记。
    # 凭据事件只记「设置了 / 清除了」，值不进审计——审计表长期保留。
    CONNECTOR_CREATED = "connector_created"
    CONNECTOR_UPDATED = "connector_updated"
    CONNECTOR_DELETED = "connector_deleted"
    CONNECTOR_CREDENTIAL_SET = "connector_credential_set"
    CONNECTOR_CREDENTIAL_CLEARED = "connector_credential_cleared"
    CONNECTOR_HEALTH_CHECKED = "connector_health_checked"
    # 绑定/解绑工具会改变「哪些智能体能碰什么」，即使没有改任何智能体。
    CONNECTOR_TOOLS_BOUND = "connector_tools_bound"
    CONNECTOR_TOOL_UNBOUND = "connector_tool_unbound"

    # 个人访问令牌：签发与撤销都要留痕。令牌**值**永不进审计——
    # 审计表长期保留，塞进去等于把凭据永久留档。
    API_TOKEN_ISSUED = "api_token_issued"
    API_TOKEN_REVOKED = "api_token_revoked"

    # 编排：图本身就是治理对象——「谁在第几步、用什么边界做什么」写在这张图里。
    # 控制台与构建助手是两个入口，两边都必须留痕；否则从控制台删掉一张图，
    # 事后既查不出谁删的、也给不了 `verify_workflow_deleted` 要的那份正向证据。
    WORKFLOW_CREATED = "workflow_created"
    WORKFLOW_UPDATED = "workflow_updated"
    WORKFLOW_STATUS_CHANGED = "workflow_status_changed"
    WORKFLOW_DELETED = "workflow_deleted"

    # 事务层：工作区与快照是持久状态，创建/归档/删除必须能事后追溯；
    # 任务状态迁移由 execution_traces 记录，这里只记治理级动作（谁发起、谁取消）。
    WORKSPACE_CREATED = "workspace_created"
    WORKSPACE_ARCHIVED = "workspace_archived"
    WORKSPACE_DELETED = "workspace_deleted"
    SNAPSHOT_CREATED = "snapshot_created"
    SNAPSHOT_DELETED = "snapshot_deleted"
    TASK_CREATED = "task_created"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_UNKNOWN = "task_unknown"

    ADMIN_ACTION = "admin_action"
    SECURITY_EVENT = "security_event"


class AuditLog(TenantScoped, Base):
    """
    追加式审计事件表。

    审计表原则：
    - 尽量只追加，不直接 UPDATE 历史事件；
    - metadata_json 仅写入脱敏字段；
    - 不写入原始认证 Token、Cookie、密码或完整模型上下文；
    - 请求参数应存摘要、哈希或必要业务字段，而非所有原文。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index(
            "ix_audit_logs_tenant_created_at",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_audit_logs_actor_created_at",
            "actor_id",
            "created_at",
        ),
        Index(
            "ix_audit_logs_event_type_created_at",
            "event_type",
            "created_at",
        ),
        Index(
            "ix_audit_logs_resource",
            "resource_type",
            "resource_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    actor_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            name="audit_event_type",
            native_enum=False,
            validate_strings=True,
            # 不写 length 时列长由最长成员名推断，新增更长的成员就会
            # 在 PostgreSQL 上截断报错（varchar(26) 的旧坑）。
            length=64,
        ),
        nullable=False,
        index=True,
    )

    resource_type: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    thread_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        index=True,
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    success: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
    )
    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    metadata_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
