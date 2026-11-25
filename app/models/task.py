# app/models/task.py
"""事务层：工作区、任务、执行记录与状态迁移轨迹。

与一次性代码执行的区别（见 docs/TRANSACTION.md）：

- 沙箱的一次 `/v1/execute` 是**执行**，对应 `TaskExecution`；
- 用户交给平台的一件可交付的事是**任务**，对应 `Task`，一个任务可有多次执行；
- 工作区是**按人持久**的目录，一次执行只挂它的快照；
- 每次执行的每一次状态迁移都进 `ExecutionTrace`（追加写），当前态进 `ExecutionState`。

租户隔离走 `TenantScoped`：四张表全部带 tenant_id，自动过滤与写入校验由
`app.tenancy.scope` 统一负责。执行记录即使在沙箱侧产生，平台侧回读也必须
带上租户上下文，不能靠猜 execution_id。

`cpu_ms` 为 NULL 表示沙箱没回报 CPU 时间，**不是 0**——0 是「确实没消耗 CPU」，
两者在计量上不能混（见 docs/TRANSACTION.md §7）。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class WorkspaceStatus(str, enum.Enum):
    """工作区生命周期。软删除保留快照与审计，不物理删目录。"""

    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"
    DELETED = "deleted"


class TaskKind(str, enum.Enum):
    DOCUMENT = "document"
    CODE = "code"
    SCHEDULE = "schedule"


class TaskStatus(str, enum.Enum):
    """任务状态机。

    QUEUED / RUNNING / AWAITING_APPROVAL / AWAITING_INPUT 是中间态；
    SUCCEEDED / FAILED / CANCELLED / UNKNOWN 是终态。

    UNKNOWN 是刻意的：进程重启后在途任务不能猜成功也不能自动重发，
    和 `SandboxTimeout` 的 `retryable=false` 是同一条纪律。
    """

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.UNKNOWN,
    }
)


class ExecutionStatus(str, enum.Enum):
    """单次执行的状态。终态值与沙箱协议一致。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _enum(cls: type[enum.Enum]) -> Enum:
    """统一按 value 落库：迁移、ORM、手写 SQL 三处口径必须一致。"""
    return Enum(
        cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class Workspace(TenantScoped, Base):
    """按人一块持久目录。单机版：root_path 是服务端生成的本地绝对路径。"""

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_workspaces_tenant_user"),
        Index("ix_workspaces_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # 服务端生成，客户端不能传；换共享存储时只替换解析处，不改表。
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    status: Mapped[WorkspaceStatus] = mapped_column(
        _enum(WorkspaceStatus),
        nullable=False,
        default=WorkspaceStatus.ACTIVE,
        server_default=WorkspaceStatus.ACTIVE.value,
    )

    # 0 = 沿用平台默认上限。
    quota_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )

    # 最近一次快照的标识；轮转清理由服务层按创建时间决定保留哪几份。
    last_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Task(TenantScoped, Base):
    """一件可交付的事。一个任务可包含多次执行。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_tenant_user_status", "tenant_id", "user_id", "status"),
        Index("ix_tasks_workspace", "workspace_id"),
        # 撤销授权要精确反查这个 schedule 下的在途任务；不靠解析
        # idempotency_key 的字符串前缀，那是编码不是关联。
        Index("ix_tasks_schedule_status", "schedule_id", "status"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind), nullable=False)

    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus),
        nullable=False,
        default=TaskStatus.QUEUED,
        server_default=TaskStatus.QUEUED.value,
    )

    # 定时触发与人工重试共用：同一租户内同键只能有一个任务。
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # 所有执行之和；NULL = 沙箱没回报，不能当 0。
    cpu_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 任务输入与产出，都以 artifact 键引用，正文不进这一行。
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 投递只认这份快照，不回读 schedule——同一份事实不放两处。撤销时由
    # revoke_delivery 级联改写它，已入队任务的投递随即失效。
    # 只记「允许调哪个工具、参数落在什么范围、何时到期」，**不记 scopes/roles**
    # ——那会让一次授权变成可反复使用的提权凭证。每次投递都重新解析用户
    # 当前 RBAC 权限，收窄立即生效（见 docs/TRANSACTION.md §8 开放项 4）。
    delivery_grant_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 来源 schedule；撤销授权时按它精确反查在途任务。一次性任务为 NULL。
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TaskExecution(TenantScoped, Base):
    """一次沙箱执行。id 就是沙箱回报的 execution_id，不是本地生成的 UUID。"""

    __tablename__ = "task_executions"
    __table_args__ = (
        Index("ix_task_executions_task", "task_id", "created_at"),
        Index("ix_task_executions_tenant_status", "tenant_id", "status"),
        UniqueConstraint("tenant_id", "execution_id", name="uq_executions_tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # 沙箱侧 execution_id；跨服务关联的唯一键。
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 任务执行才有 task_id；对话里的一次性代码执行没有所属任务。
    # 沙箱直写时不会带这个值，因此必须可空（迁移 d4a7c9e13b62）。
    task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus),
        nullable=False,
        default=ExecutionStatus.QUEUED,
        server_default=ExecutionStatus.QUEUED.value,
    )

    # 沙箱侧 cgroup 差值换算；缺字段为 NULL，不是 0。
    cpu_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 这次执行用的策略版本，与发出时提交的版本比对，不一致不计入结论。
    policy_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 执行前打的那份快照；失败时按它回滚。
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # stdout/stderr 放对象存储，这里只留键与截断标记。
    stdout_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stderr_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    truncated: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )

    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionTrace(TenantScoped, Base):
    """状态迁移轨迹，追加写、永不更新。

    与 `ExecutionState` 分开：trace 是事实日志（发生了什么），state 是当前快照
    （现在是什么）。审批回读要的是两者一致，而不是只信执行时那一次响应。
    """

    __tablename__ = "execution_traces"
    __table_args__ = (
        Index("ix_execution_traces_execution_seq", "execution_id", "seq"),
        UniqueConstraint("tenant_id", "execution_id", "seq", name="uq_trace_execution_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)

    # 谁触发的迁移：sandbox / platform / user / scheduler。
    actor: Mapped[str] = mapped_column(String(32), nullable=False)

    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionState(TenantScoped, Base):
    """一次执行的当前状态快照。每次迁移与 trace 在同一事务里更新。"""

    __tablename__ = "execution_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_id", name="uq_state_execution"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Schedule(TenantScoped, Base):
    """定时触发器。

    触发时**没有请求上下文**：必须先从本行取 (tenant_id, user_id)，构造独立的
    contextvars.Context 并 bind_tenant，再创建任务。不得用 system_tenant_scope
    图省事——那会让所有定时任务以系统租户写库、审计全丢（见 docs/TRANSACTION.md §7）。

    幂等靠 `(schedule_id, next_fire_at)` 生成的 idempotency_key：同一触发点
    重复扫描只会得到一个任务；进度靠 next_fire_at 推进，不靠任务是否成功——
    失败的任务照常推进，避免一个坏任务卡死后续所有触发。
    """

    __tablename__ = "schedules"
    __table_args__ = (
        Index("ix_schedules_due", "enabled", "next_fire_at"),
        Index("ix_schedules_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind), nullable=False)

    # 标准 5 段 cron（分 时 日 月 周），按 timezone_offset_hours 解释。
    cron: Mapped[str] = mapped_column(String(128), nullable=False)
    # 相对 UTC 的小时偏移；8 = 北京时间。不依赖 zoneinfo/tzdata，部署更省事。
    timezone_offset_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8, server_default="8"
    )

    # UTC 时刻；调度器扫描 next_fire_at <= now 且 enabled 的行。
    next_fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )

    # 生成任务时透传给 task.input_json 的模板；凭据永不进这里。
    task_template_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 一次授权，覆盖本 schedule 的所有未来触发。按 schedule 而不是按人/按
    # workspace：按人太宽（此人所有定时任务共享），按 workspace 会跨任务串权。
    # 同样不存 scopes/roles，见 Task.delivery_grant_json 的注释。
    delivery_grant_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
