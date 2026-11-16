# app/services/execution_recovery_service.py
"""卡住的 EXECUTING 记录转入 RECOVERY_REQUIRED。

绝不自动重发写操作：进程崩溃时无法判断外部系统是否已受理，
只能标记为待恢复，由人工或回读任务确认。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditEventType
from app.services.audit_service import write_audit_event

logger = logging.getLogger(__name__)


async def mark_stale_executions_for_recovery(
    *,
    stale_after_seconds: int | None = None,
    limit: int = 200,
) -> int:
    threshold = datetime.now(UTC) - timedelta(
        seconds=stale_after_seconds or settings.executing_stale_seconds
    )

    async with async_session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.status == ApprovalStatus.EXECUTING)
                    .where(ApprovalRequest.execution_started_at <= threshold)
                    .limit(limit)
                )
            ).all()

            for approval in rows:
                approval.status = ApprovalStatus.RECOVERY_REQUIRED
                approval.recovery_note = (
                    "执行状态超过安全阈值；系统不会自动重试。"
                    "必须通过外部回读或幂等键核查实际结果。"
                )

                await write_audit_event(
                    session,
                    event_type=AuditEventType.APPROVAL_RECOVERY_REQUIRED,
                    tenant_id=approval.tenant_id,
                    actor_id="system",
                    resource_type="approval",
                    resource_id=str(approval.id),
                    thread_id=approval.thread_id,
                    run_id=approval.run_id,
                    success=False,
                    message="执行状态未知，已转入待恢复。",
                    metadata={
                        "tool_name": approval.tool_name,
                        "idempotency_key": approval.idempotency_key,
                        "external_operation_id": approval.external_operation_id,
                    },
                )

    if rows:
        logger.warning(
            "执行超时记录已转入恢复队列。",
            extra={"event": "execution_recovery_required", "count": len(rows)},
        )

    return len(rows)
