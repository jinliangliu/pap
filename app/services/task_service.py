# app/services/task_service.py
"""事务本体：任务状态机、幂等入队、在途恢复与取消。

三条纪律（见 docs/TRANSACTION.md）：

1. **迁移只能走合法边**。非法跳转（比如 succeeded -> running）直接拒绝，
   不静默改写——状态机一旦能随便跳，"当前状态"就不再是可信事实。
2. **unknown 不猜、不自动重发**。进程重启后在途任务标 unknown，等人工确认；
   这与 `SandboxTimeout` 的 `retryable=false` 是同一条纪律。
3. **取消是请求，不是即时状态改写**。`cancel_requested_at` 落时间戳，
   由执行方在下一个检查点自行收口；直接把 running 改成 cancelled 会造成
   "库里取消了、沙箱还在跑"的双事实。

`cpu_ms` 累加时 NULL 不能被 0 吃掉：只要有一次执行没回报，总里程就不可信，
保持 NULL。只有全部执行都回报了，才给出累加值。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.audit import AuditEventType
from app.models.task import (
    TERMINAL_TASK_STATUSES,
    Task,
    TaskKind,
    TaskStatus,
)
from app.services.audit_service import write_audit_event

logger = logging.getLogger(__name__)

# 合法迁移边。终态没有任何出边——重开一个任务必须新建，用 idempotency_key
# 之外的新键；这样历史状态不会被改写，审计链保持完整。
_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.UNKNOWN}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.AWAITING_INPUT,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.UNKNOWN,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.UNKNOWN}
    ),
    TaskStatus.AWAITING_INPUT: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.UNKNOWN}
    ),
    # 终态：无出边。
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.UNKNOWN: frozenset(),
}


class TaskError(RuntimeError):
    """任务状态或参数不合法。"""


class InvalidTransition(TaskError):
    """状态迁移不在合法边集合内。"""


class TaskNotFound(TaskError):
    """任务不存在或不属于当前用户。"""


def _enum_value(status: TaskStatus | str) -> TaskStatus:
    return status if isinstance(status, TaskStatus) else TaskStatus(status)


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


async def create_task(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: uuid.UUID,
    kind: TaskKind,
    idempotency_key: str,
    input_json: dict | None = None,
    delivery_grant_json: dict | None = None,
    schedule_id: uuid.UUID | None = None,
    deadline_at: datetime | None = None,
    actor_id: str | None = None,
) -> tuple[Task, bool]:
    """幂等入队。返回 (任务, 是否新建)。

    同租户内同 idempotency_key 只应有一个任务：定时触发重复、人工重试、
    消息重投都靠它挡住。撞已有任务时返回 `created=False`，不报错、不重建。
    """
    existing = await session.scalar(
        select(Task).where(Task.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing, False

    row = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        kind=kind,
        status=TaskStatus.QUEUED,
        idempotency_key=idempotency_key,
        input_json=input_json,
        delivery_grant_json=delivery_grant_json,
        schedule_id=schedule_id,
        deadline_at=deadline_at,
    )
    session.add(row)
    await session.flush()

    await write_audit_event(
        session,
        event_type=AuditEventType.TASK_CREATED,
        tenant_id=tenant_id,
        actor_id=actor_id or user_id,
        resource_type="task",
        resource_id=str(row.id),
        message=f"任务入队：{kind.value}。",
        metadata={
            "user_id": user_id,
            "workspace_id": str(workspace_id),
            "idempotency_key": idempotency_key,
            "kind": kind.value,
        },
    )
    await session.flush()
    return row, True


async def transition(
    session: AsyncSession,
    task: Task,
    target: TaskStatus,
    *,
    actor_id: str = "platform",
    note: str | None = None,
) -> Task:
    """把任务迁移到 target，非法边直接拒绝。"""
    current = _enum_value(task.status)
    goal = _enum_value(target)

    if not can_transition(current, goal):
        raise InvalidTransition(
            f"任务 {task.id} 不允许从 {current.value} 迁移到 {goal.value}。"
        )

    task.status = goal
    task.updated_at = datetime.now(UTC)

    if goal in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNKNOWN):
        event = {
            TaskStatus.CANCELLED: AuditEventType.TASK_CANCELLED,
            TaskStatus.UNKNOWN: AuditEventType.TASK_UNKNOWN,
        }.get(goal)
        if event is not None:
            await write_audit_event(
                session,
                event_type=event,
                tenant_id=task.tenant_id,
                actor_id=actor_id,
                resource_type="task",
                resource_id=str(task.id),
                message=note or f"任务状态：{current.value} -> {goal.value}。",
                metadata={"from": current.value, "to": goal.value, "user_id": task.user_id},
            )

    await session.flush()
    return task


async def request_cancel(
    session: AsyncSession,
    *,
    task: Task,
    actor_id: str,
) -> Task:
    """登记取消请求。真正终止由执行方在检查点调用 transition(CANCELLED)。"""
    if task.status in TERMINAL_TASK_STATUSES:
        raise InvalidTransition(f"任务 {task.id} 已是终态，不能取消。")

    if task.cancel_requested_at is None:
        task.cancel_requested_at = datetime.now(UTC)
        await write_audit_event(
            session,
            event_type=AuditEventType.TASK_CANCELLED,
            tenant_id=task.tenant_id,
            actor_id=actor_id,
            resource_type="task",
            resource_id=str(task.id),
            message="取消请求已登记，等待执行方收口。",
            metadata={"user_id": task.user_id, "status": task.status.value},
        )
        await session.flush()
    return task


def accumulate_cpu_ms(current: int | None, executed: int | None) -> int | None:
    """累加各次执行的 CPU 时间，保持 NULL 语义。

    `current=None` 有两种历史含义：任务还没执行过（初值），或某次执行没回报
    （不可信累计）。对**任务**来说终态无出边、只有一次执行，所以把 None
    当「还没有有效累计」即可：首次回报直接落值；一旦某次没回报就保持 None，
    不让部分里程冒充总里程。`executed=None` 无论当前值如何都落 None。
    """
    if executed is None:
        return None
    if current is None:
        return executed
    return current + executed


async def record_execution_cpu_ms(
    session: AsyncSession,
    *,
    task: Task,
    cpu_ms: int | None,
) -> Task:
    """把一次执行的 CPU 时间并入任务总计，保持 NULL 语义。"""
    task.cpu_ms = accumulate_cpu_ms(task.cpu_ms, cpu_ms)
    await session.flush()
    return task


async def mark_stale_tasks_unknown(
    *,
    stale_after_seconds: int | None = None,
    limit: int = 200,
) -> int:
    """把超时未收口的在途任务标 unknown。

    调用方负责租户上下文（维护任务逐租户调用）。`created_at` 作为心跳的
    近似：任务每次状态迁移都会更新 `updated_at`，用它在途判断更准。
    """
    threshold = datetime.now(UTC) - timedelta(
        seconds=stale_after_seconds or settings.task_stale_after_seconds
    )
    stale_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.AWAITING_INPUT,
    ]

    marked = 0
    async with async_session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(Task)
                    .where(Task.status.in_(stale_statuses))
                    .where(Task.updated_at <= threshold)
                    .order_by(Task.updated_at)
                    .limit(limit)
                )
            ).all()

            for task in rows:
                previous = task.status
                task.status = TaskStatus.UNKNOWN
                task.updated_at = datetime.now(UTC)
                marked += 1

                await write_audit_event(
                    session,
                    event_type=AuditEventType.TASK_UNKNOWN,
                    tenant_id=task.tenant_id,
                    actor_id="system",
                    resource_type="task",
                    resource_id=str(task.id),
                    message=(
                        f"在途任务超过 {settings.task_stale_after_seconds}s 未收口，"
                        "标记 unknown；系统不自动重发。"
                    ),
                    metadata={
                        "from": previous.value,
                        "to": TaskStatus.UNKNOWN.value,
                        "user_id": task.user_id,
                    },
                )

    if marked:
        logger.warning(
            "在途任务已标记 unknown。",
            extra={"event": "task_marked_unknown", "count": marked},
        )
    return marked


async def get_owned(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: str,
) -> Task:
    """按 id 取任务，并校验属于当前用户。"""
    task = await session.get(Task, task_id)
    if task is None or task.user_id != user_id:
        raise TaskNotFound("任务不存在或不属于当前用户。")
    return task
