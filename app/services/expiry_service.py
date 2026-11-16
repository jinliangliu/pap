# app/services/expiry_service.py
"""把超时未确认的审批置为 EXPIRED。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.checkpoint import discard_thread
from app.db.session import async_session_factory
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditEventType
from app.services.audit_service import write_audit_event
from app.services.run_service import close_runs_for_approval

logger = logging.getLogger(__name__)


async def expire_pending_approvals(*, limit: int = 200) -> int:
    now = datetime.now(UTC)

    async with async_session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.status == ApprovalStatus.PENDING)
                    .where(ApprovalRequest.expires_at <= now)
                    .limit(limit)
                )
            ).all()

            for approval in rows:
                approval.status = ApprovalStatus.EXPIRED
                approval.completed_at = now

                await write_audit_event(
                    session,
                    event_type=AuditEventType.APPROVAL_EXPIRED,
                    tenant_id=approval.tenant_id,
                    actor_id="system",
                    resource_type="approval",
                    resource_id=str(approval.id),
                    thread_id=approval.thread_id,
                    run_id=approval.run_id,
                    success=False,
                    metadata={"tool_name": approval.tool_name},
                )

    if rows:
        # 单子过期了，那一轮「等审批」的运行也就永远等不到人了。过去这里只改单子、
        # 不管运行，于是看板上一直挂着点不动的待办（2026-11-13 线上积了 5 条）。
        # 放在事务外逐条收口：收口是独立事务，且只作用于仍是等待态的运行。
        for approval in rows:
            await close_runs_for_approval(
                approval_id=str(approval.id),
                run_id=approval.run_id or None,
                tenant_id=approval.tenant_id,
                status=ApprovalStatus.EXPIRED,
            )

        # 委派来的审批过期后，子线程的检查点不会再有人用：恢复点只对
        # 「从这一跳继续执行」有意义，而这张单子已经不可能被确认。
        # 不清理的话，每张过期单都会在 checkpoint 表里留一条永远不对应
        # 任何会话的线程——P2 之前不存在这个问题，因为子调用总是当场跑完。
        for approval in rows:
            if approval.delegation_chain:
                await discard_thread(approval.thread_id)

        logger.info(
            "过期审批已处理。",
            extra={"event": "approvals_expired", "count": len(rows)},
        )

    return len(rows)
