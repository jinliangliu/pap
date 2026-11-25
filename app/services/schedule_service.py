# app/services/schedule_service.py
"""定时触发：due 扫描、独立租户上下文构造、幂等入队与进度推进。

三条纪律（见 docs/TRANSACTION.md §7）：

1. **定时触发没有请求上下文**。绝不能直接写库——那样 tenant_id 为空，RLS
   会拒绝写入（或更糟：以 system 租户写入，审计全丢）。必须先从 schedules
   行取 (tenant_id, user_id)，构造独立 Context，bind_tenant，再起任务。
2. **不得用 system_tenant_scope 图省事**。system 只用于「读出所有 due 行」，
   一旦进入写路径就必须收窄到具体租户。
3. **进度与任务成败解耦**。触发后立刻推进 next_fire_at 并留 last_task_id；
   任务失败照常推进，否则一个坏任务会卡死后续所有触发。幂等键由
   (schedule_id, 触发时刻) 生成，重复扫描只会得到一个任务。

单实例靠 maintenance 循环已有的 advisory lock（本服务由它调用，不另起锁）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextvars import copy_context
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.task import Schedule, TaskKind
from app.models.tenant import Tenant, TenantStatus
from app.tenancy.context import (
    TenantIsolationError,
    bind_tenant,
    current_tenant,
    system_tenant_scope,
    tenant_scope,
)

logger = logging.getLogger(__name__)

# cron 五年内找不到下一个匹配就报错——正常 5 段表达式不会这么稀疏，
# 死循环往往意味着表达式写错（比如 2 月 30 日）。
_MAX_SCAN_DAYS = 366 * 5


class CronError(ValueError):
    """cron 表达式无效或没有可计算的下一个触发点。"""


def _parse_field(field: str, low: int, high: int) -> frozenset[int]:
    """解析一段 cron：`*` / `*/n` / `a-b` / `a,b` / 单值。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError("cron 字段含空项。")

        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"cron 步长无效：{part}") from exc
            if step < 1:
                raise CronError(f"cron 步长必须大于 0：{part}")
        else:
            base = part

        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, right = base.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise CronError(f"cron 区间无效：{part}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronError(f"cron 数值无效：{part}") from exc

        if start < low or end > high or start > end:
            raise CronError(f"cron 数值越界：{part}（允许 {low}-{high}）")
        values.update(range(start, end + 1, step))

    return frozenset(values)


def parse_cron(expression: str) -> tuple[frozenset[int], ...]:
    """解析标准 5 段 cron，返回 (分, 时, 日, 月, 周)。

    周字段用 cron 口径：0=周日，1=周一…6=周六（与 Python weekday 差 1）。
    """
    fields = (expression or "").split()
    if len(fields) != 5:
        raise CronError("cron 必须是 5 段：分 时 日 月 周。")

    return (
        _parse_field(fields[0], 0, 59),
        _parse_field(fields[1], 0, 23),
        _parse_field(fields[2], 1, 31),
        _parse_field(fields[3], 1, 12),
        _parse_field(fields[4], 0, 6),
    )


def next_fire_at(
    expression: str,
    *,
    after: datetime,
    timezone_offset_hours: int,
) -> datetime:
    """算出 after 之后的下一次触发时刻（UTC）。

    cron 按 `utc + offset` 的本地时间解释；存回库的一律是 UTC。
    逐分钟扫描，简单且对 5 段 cron 足够快；五年上限只防表达式写错。
    """
    minutes, hours, days, months, weekdays = parse_cron(expression)
    # SQLite 读回的 datetime 是 naive，astimezone() 会把它当成本机时区
    # （开发机 GMT+8 时结果整体偏移 8 小时，下一次触发算到过去）。库里存的
    # 语义始终是 UTC，所以 naive 一律按 UTC 补齐再算。
    aware = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
    local = aware.astimezone(UTC) + timedelta(hours=timezone_offset_hours)
    candidate = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = candidate + timedelta(days=_MAX_SCAN_DAYS)

    while candidate < deadline:
        # Python weekday: 0=周一…6=周日；cron: 0=周日…6=周六。
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.day in days
            and candidate.month in months
            and cron_weekday in weekdays
        ):
            return (candidate - timedelta(hours=timezone_offset_hours)).astimezone(UTC)
        candidate += timedelta(minutes=1)

    raise CronError(f"cron 表达式 {expression!r} 在五年内没有可匹配的触发点。")


def make_idempotency_key(schedule_id: uuid.UUID, fire_at: datetime) -> str:
    """同一 schedule 的同一触发时刻只生成一个任务。"""
    aware = fire_at if fire_at.tzinfo is not None else fire_at.replace(tzinfo=UTC)
    stamp = aware.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"schedule:{schedule_id}:{stamp}"


async def _fire_one(schedule_id: uuid.UUID, tenant_id: str, fire_at: datetime) -> None:
    """在已绑定的租户上下文里入队一个任务，并推进进度。

    注意：本协程是在 `copy_context()` + `bind_tenant()` 的独立 Context 里被
    create_task 调起的，进入时租户已经是 schedule 的所属租户。这里再断言一次，
    防止将来有人不改上下文就直接调用本函数。
    """
    from app.services.task_service import create_task

    if current_tenant() != tenant_id:
        raise TenantIsolationError(
            f"定时触发上下文错误：期望 {tenant_id}，实际 {current_tenant()!r}。"
        )

    async with async_session_factory() as session:
        async with session.begin():
            row = await session.get(Schedule, schedule_id)
            if row is None or not row.enabled:
                return

            # 幂等窗口：只有当行上的 fire_at 仍未越过这次触发点时才入队。
            # 重复扫描或上一轮推进失败重扫，都靠这里挡住双份任务。
            if row.next_fire_at > fire_at:
                return

            task, created = await create_task(
                session,
                tenant_id=tenant_id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                kind=TaskKind.SCHEDULE,
                idempotency_key=make_idempotency_key(schedule_id, fire_at),
                input_json=row.task_template_json,
                # 触发时快照授权；权限（scopes/roles）不快照，每次现取。
                # 撤销会级联改写这份快照，所以已入队的任务也会被即时打断。
                delivery_grant_json=row.delivery_grant_json,
                schedule_id=schedule_id,
                actor_id="scheduler",
            )

            next_at = next_fire_at(
                row.cron,
                after=fire_at,
                timezone_offset_hours=row.timezone_offset_hours,
            )
            row.next_fire_at = next_at
            row.last_fire_at = fire_at
            row.last_task_id = task.id
            row.updated_at = datetime.now(UTC)

            logger.info(
                "定时任务触发。",
                extra={
                    "event": "schedule_fired",
                    "schedule_id": str(schedule_id),
                    "task_id": str(task.id),
                    "created": created,
                    "next_fire_at": next_at.isoformat(),
                },
            )


async def fire_due_schedules(
    *,
    now: datetime | None = None,
    limit: int = 200,
    tenant_ids: list[str] | None = None,
) -> int:
    """扫描并触发所有到期 schedule，返回实际入队的条数。

    调用方负责单实例（maintenance 的 advisory lock），并传入活跃租户列表
    （它本来就要逐租户扫，避免重复查询）。`tenant_ids=None` 时自行读取，
    供独立入口/测试使用。

    **逐租户**读 due 行：schedules 挂了 FORCE RLS，system scope 下不设
    app.tenant_id，直接查会因策略返回空集——SQLite 测试没有 RLS 所以不会
    暴露，PG 上会静默漏掉全部定时任务。写入阶段每条都在独立租户 Context
    里执行，system 绝不进入写路径。
    """
    now = now or datetime.now(UTC)

    if tenant_ids is None:
        with system_tenant_scope():
            async with async_session_factory() as session:
                tenant_ids = list(
                    await session.scalars(
                        select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)
                    )
                )

    dues: list[tuple[uuid.UUID, str, datetime]] = []
    for tenant_id in tenant_ids:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                rows = (
                    await session.scalars(
                        select(Schedule)
                        .where(Schedule.enabled.is_(True))
                        .where(Schedule.next_fire_at <= now)
                        .order_by(Schedule.next_fire_at)
                        .limit(limit)
                    )
                ).all()
                dues.extend(
                    (row.id, row.tenant_id, row.next_fire_at) for row in rows
                )

    if not dues:
        return 0

    # 写阶段：每条一个独立 Context。copy_context() 复制当前（可能是 system）
    # 上下文，再用 bind_tenant 收窄到具体租户——这是定时触发唯一合法的写法。
    tasks: list[asyncio.Task] = []
    for schedule_id, tenant_id, fire_at in dues:
        context = copy_context()
        context.run(bind_tenant, tenant_id)
        tasks.append(
            asyncio.create_task(
                _fire_one(schedule_id, tenant_id, fire_at),
                context=context,
                name=f"schedule-fire-{schedule_id}",
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    fired = 0
    for (schedule_id, tenant_id, _), result in zip(dues, results, strict=True):
        if isinstance(result, Exception):
            logger.error(
                "定时触发失败，本轮跳过；下轮会重试。",
                extra={
                    "event": "schedule_fire_failed",
                    "schedule_id": str(schedule_id),
                    "tenant_id": tenant_id,
                    "error": str(result),
                },
            )
        else:
            fired += 1

    return fired
