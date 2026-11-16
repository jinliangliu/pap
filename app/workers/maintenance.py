# app/workers/maintenance.py
"""后台维护任务：过期审批、卡死执行记录。

使用 asyncio 任务而不是 APScheduler：少一个依赖，生命周期由 lifespan 管理。

多实例部署时每个进程都会起这个循环，但同一时刻只应该有一个实例在扫：
用 PostgreSQL 事务级 advisory lock 做租约式互斥——抢到锁的实例执行本轮，
没抢到的直接跳过。事务结束锁自动释放，不需要额外的释放逻辑，
进程崩溃也不会留下永久锁。

SQLite（本地/测试）没有 advisory lock，此时单进程运行，直接执行。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.tenant import Tenant, TenantStatus
from app.services.connector_service import purge_orphan_credentials
from app.services.execution_recovery_service import (
    mark_stale_executions_for_recovery,
)
from app.services.expiry_service import expire_pending_approvals
from app.services.obs_sweep import sweep_orphaned_runs
from app.services.run_service import reconcile_waiting_runs
from app.tenancy.context import system_tenant_scope, tenant_scope

logger = logging.getLogger(__name__)

_LEADER_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(:key)"


async def _active_tenants() -> list[str]:
    async with async_session_factory() as session:
        rows = await session.scalars(
            select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)
        )
        return list(rows)


async def _run_sweeps() -> None:
    """
    逐租户扫描。

    不能一次跨租户扫描：应用层会自动追加 tenant_id 条件，
    PostgreSQL 的 RLS 策略也要求事务里设置了 app.tenant_id。
    「跨租户」不是维护任务的默认能力，而是逐租户遍历出来的结果。
    """
    expired = 0
    recovered = 0
    purged = 0
    swept = 0
    reconciled = 0

    try:
        with system_tenant_scope():
            tenants = await _active_tenants()

        for tenant_id in tenants:
            with tenant_scope(tenant_id):
                expired += await expire_pending_approvals()
                recovered += await mark_stale_executions_for_recovery()
                purged += await purge_orphan_credentials()
                swept += await sweep_orphaned_runs()
                # 排在过期之后：本轮的过期单先落终态，对账这一遍就能顺带收口。
                reconciled += await reconcile_waiting_runs()
    except Exception:
        logger.exception(
            "维护任务执行失败。",
            extra={"event": "maintenance_failed"},
        )
        return

    if expired or recovered or purged or swept or reconciled:
        logger.info(
            "维护任务完成。",
            extra={
                "event": "maintenance_completed",
                "expired": expired,
                "recovery_required": recovered,
                "credentials_purged": purged,
                "obs_orphans": swept,
                "runs_reconciled": reconciled,
            },
        )


async def _try_become_leader() -> tuple[bool, AsyncSession | None]:
    """
    尝试拿本轮维护的执行权。

    返回 (是否执行, 持有锁的会话)。会话必须在扫描结束后提交/回滚，
    否则锁会一直被这个事务占着，其他实例永远抢不到。
    """
    if not (settings.maintenance_require_leader and settings.is_postgres):
        return True, None

    session = async_session_factory()

    try:
        acquired = bool(
            await session.scalar(
                text(_LEADER_LOCK_SQL),
                {"key": settings.maintenance_advisory_lock_key},
            )
        )
    except Exception:
        await session.rollback()
        await session.close()

        logger.warning(
            "获取维护锁失败，本轮跳过。",
            extra={"event": "maintenance_lock_error"},
            exc_info=True,
        )
        return False, None

    if not acquired:
        await session.rollback()
        await session.close()

        logger.debug(
            "其他实例正在执行维护，本轮跳过。",
            extra={"event": "maintenance_skipped_not_leader"},
        )
        return False, None

    return True, session


async def run_maintenance_once() -> None:
    proceed, lock_session = await _try_become_leader()

    if not proceed:
        return

    try:
        await _run_sweeps()
    finally:
        if lock_session is not None:
            # 提交事务即释放 advisory lock。
            await lock_session.commit()
            await lock_session.close()


async def maintenance_loop(interval_seconds: int | None = None) -> None:
    interval = interval_seconds or settings.maintenance_interval_seconds

    while True:
        await asyncio.sleep(interval)
        await run_maintenance_once()


def start_maintenance_task() -> asyncio.Task | None:
    if not settings.maintenance_enabled:
        return None

    return asyncio.create_task(
        maintenance_loop(),
        name="approval-maintenance",
    )


async def stop_maintenance_task(task: asyncio.Task | None) -> None:
    if task is None:
        return

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
