# app/services/run_service.py
"""会话、消息与运行记录的持久化。"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import async_session_factory
from app.models.approval import (
    TERMINAL_APPROVAL_STATUSES,
    ApprovalRequest,
    ApprovalStatus,
)
from app.models.conversation import (
    AgentRun,
    Conversation,
    Message,
    MessageRole,
    RunStatus,
)
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)


async def ensure_conversation(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    thread_id: str,
    title: str | None = None,
    channel: str = "api",
) -> Conversation:
    """取或建一条会话。

    `channel` 只在**创建时**写一次，已有会话不重写：它是「这条会话从哪个入口
    开始的」，不是「最近一条消息从哪发的」。每次请求都改的话，同一条线程被
    两个入口用着时会来回跳标签，历史看起来像丢过一段。
    """
    with tenant_scope(tenant_id):
        conversation = await session.scalar(
            select(Conversation).where(Conversation.thread_id == thread_id)
        )

        if conversation is not None:
            if conversation.tenant_id != tenant_id or conversation.user_id != user_id:
                raise PermissionError("该会话不属于当前用户。")

            conversation.last_active_at = datetime.now(UTC)
            return conversation

        conversation = Conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            title=(title or "")[:256] or None,
            channel=channel,
            last_active_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()

        return conversation


async def add_message(
    session: AsyncSession,
    *,
    tenant_id: str,
    conversation_id: uuid.UUID,
    role: MessageRole,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    with tenant_scope(tenant_id):
        message = Message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
            # 显式写入带微秒的时间：建表默认值是秒级，同一轮问答的
            # user / assistant 会落在同一秒，历史回放顺序会变随机。
            created_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()

        return message


async def start_run(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    thread_id: str,
    conversation_id: uuid.UUID,
    input_summary: str,
) -> AgentRun:
    with tenant_scope(tenant_id):
        run = AgentRun(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            conversation_id=conversation_id,
            status=RunStatus.RUNNING,
            input_summary=input_summary[:2000],
        )
        session.add(run)
        await session.flush()

        return run


async def finish_run(
    *,
    run_id: str,
    status: RunStatus,
    intent: str | None = None,
    tool_name: str | None = None,
    approval_id: str | None = None,
    error_category: str | None = None,
    output_summary: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """由图中的 persist_state 节点调用，独立事务，失败不影响用户答复。"""
    factory = session_factory or async_session_factory

    try:
        async with factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, uuid.UUID(run_id))
                if run is None:
                    return

                run.status = status
                run.intent = intent
                run.tool_name = tool_name
                run.approval_id = approval_id
                run.error_category = error_category
                run.output_summary = (output_summary or "")[:2000] or None
                run.finished_at = datetime.now(UTC)
    except Exception:
        logger.exception("写入运行记录失败。", extra={"event": "run_persist_failed"})


# 审批终态 → 运行的终态。
#
# 不一律记成 COMPLETED：`WAITING_FOR_APPROVAL` 的语义是「这一轮停在这里等人点」。
# 人点了拒绝、或者单子过期，这一轮什么都没执行——记成 COMPLETED 会让看板上的
# 错误率与 p95 好看得离谱，问题不是消失了，只是换个地方藏着。
#
# EXECUTED 算完成：外部系统已受理，只是没拿到回读核验。把它记成失败同样是撒谎，
# 只是撒向反方向。
_APPROVAL_RUN_OUTCOME: dict[ApprovalStatus, tuple[RunStatus, str | None, str]] = {
    ApprovalStatus.VERIFIED: (RunStatus.COMPLETED, None, "审批已执行并通过回读核验。"),
    ApprovalStatus.EXECUTED: (RunStatus.COMPLETED, None, "审批已执行，未获回读核验。"),
    ApprovalStatus.FAILED: (RunStatus.FAILED, "approval_failed", "审批执行失败。"),
    ApprovalStatus.EXECUTION_UNKNOWN: (
        RunStatus.FAILED,
        "approval_execution_unknown",
        "审批执行结果未知，需人工核查外部系统。",
    ),
    ApprovalStatus.RECOVERY_REQUIRED: (
        RunStatus.FAILED,
        "approval_recovery_required",
        "审批执行异常，需按幂等键回读恢复。",
    ),
    ApprovalStatus.REJECTED: (RunStatus.CANCELLED, "approval_rejected", "用户取消了该操作。"),
    ApprovalStatus.EXPIRED: (RunStatus.CANCELLED, "approval_expired", "审批已过期，操作未执行。"),
}


async def close_runs_for_approval(
    *,
    approval_id: str,
    run_id: str | None,
    tenant_id: str,
    status: ApprovalStatus,
    error_category: str | None = None,
    output_summary: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """审批到达终态时，把还挂在「等审批」的运行收口。

    两条关联都要匹配，因为库里两种写法同时存在：

    - `agent_runs.approval_id` —— 发起方线程的运行。这是**唯一可靠**的一条：
      早期版本的 `approval_requests.run_id` 是空串，只按 `run_id` 找会漏掉；
    - `approval_requests.run_id` —— 审批在子线程里执行时，子线程自己的运行。

    只更新仍处于 `WAITING_FOR_APPROVAL` 的行，条件写进 UPDATE 的 WHERE 而不是
    先读再改：并发下（用户点确认 + 维护任务同时在跑）读-改-写会把一次已经收口的
    运行再改一遍，并覆盖掉它真正的原因。`rowcount` 也顺带告诉调用方「有没有真的改到」。

    独立事务、失败只记日志：审批已经落库了，收口运行失败不该回滚前者。
    """
    outcome = _APPROVAL_RUN_OUTCOME.get(status)
    if outcome is None:
        return 0

    run_status, default_category, default_summary = outcome
    factory = session_factory or async_session_factory

    conditions = [AgentRun.approval_id == approval_id]
    if run_id:
        try:
            conditions.append(AgentRun.id == uuid.UUID(run_id))
        except ValueError:
            # 早期数据的 run_id 是空串或非 UUID，靠 approval_id 那条匹配即可。
            pass

    try:
        with tenant_scope(tenant_id):
            async with factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(AgentRun)
                        .where(AgentRun.status == RunStatus.WAITING_FOR_APPROVAL)
                        .where(or_(*conditions))
                        .values(
                            status=run_status,
                            error_category=error_category or default_category,
                            output_summary=output_summary or default_summary,
                            finished_at=datetime.now(UTC),
                        )
                    )
                    changed = result.rowcount or 0
    except Exception:
        logger.warning(
            "审批收口时写入运行记录失败，跳过。",
            extra={
                "event": "run_close_for_approval_failed",
                "approval_id": approval_id,
            },
            exc_info=True,
        )
        return 0

    if changed:
        logger.info(
            "审批到达终态，已收口等待中的运行。",
            extra={
                "event": "runs_closed_for_approval",
                "approval_id": approval_id,
                "status": run_status.value,
                "count": changed,
            },
        )

    return changed


async def reconcile_waiting_runs(
    *,
    grace_minutes: int = 10,
    limit: int = 200,
) -> int:
    """兜底对账：把「等审批」但永远不会再有人点的运行补成终态。

    正常路径由 `close_runs_for_approval` 在审批落终态的那一刻收口。这里扫的是
    它没能覆盖到的情况：

    - 修复前留下的历史脏行（审批单早已 VERIFIED / EXPIRED，运行还挂着）；
    - 审批单被人从库里直接改、或某次收口写库失败；
    - 进程在「审批落库」与「运行收口」之间被杀。

    只处理超过 `grace_minutes` 的运行：刚进入等待的几千毫秒里，审批可能还在
    另一个事务里没提交，扫得越勤越容易误判。在等待期内（PENDING / APPROVED /
    EXECUTING）的单子一律不动——那才是真的有人在点。

    调用方负责租户上下文（维护任务逐租户调用）。
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(minutes=grace_minutes)
    closed = 0

    async with async_session_factory() as session:
        runs = (
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.status == RunStatus.WAITING_FOR_APPROVAL)
                .where(AgentRun.started_at <= cutoff)
                .order_by(AgentRun.started_at)
                .limit(limit)
            )
        ).all()

        links: list[tuple[AgentRun, ApprovalRequest | None]] = []

        for run in runs:
            approval: ApprovalRequest | None = None

            if run.approval_id:
                approval = await session.scalar(
                    select(ApprovalRequest).where(
                        ApprovalRequest.id == _as_uuid(run.approval_id)
                    )
                )
            else:
                # 早期数据没有 approval_id，只能反查「哪张单子开在这次运行上」。
                approval = await session.scalar(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.run_id == str(run.id))
                    .order_by(ApprovalRequest.created_at.desc())
                )

            links.append((run, approval))

    for run, approval in links:
        if approval is None:
            # 没有任何审批单：这一轮既没执行、也不知道该等什么。记失败而不是完成，
            # 因为「没有单子」本身是异常，值得在统计里显形。
            closed += await close_runs_for_approval(
                approval_id=str(run.approval_id or ""),
                run_id=str(run.id),
                tenant_id=run.tenant_id,
                status=ApprovalStatus.FAILED,
                error_category="approval_missing",
                output_summary="未找到对应的审批单，该操作未执行。",
            )
            continue

        if approval.status not in TERMINAL_APPROVAL_STATUSES:
            continue

        closed += await close_runs_for_approval(
            approval_id=str(approval.id),
            run_id=str(run.id),
            tenant_id=run.tenant_id,
            status=approval.status,
        )

    return closed


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


async def interrupt_stale_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """
    进程启动时把上一轮遗留的 RUNNING 标记为中断。

    请求是进程内的协程，进程一退出它们就消失了，但状态会留在 RUNNING：
    监控据此以为还有请求在跑，而且这些记录永远等不到 finish_run。
    2026-11-04 那次死循环重启后就残留了一条。

    必须逐租户处理：agent_runs 挂了 RLS，无租户上下文时 UPDATE 影响 0 行，
    而且会静默成功——不报错、也不改任何东西。
    """
    from sqlalchemy import update

    from app.models.tenant import Tenant

    factory = session_factory or async_session_factory
    total = 0

    try:
        async with factory() as session:
            async with session.begin():
                tenant_ids = (await session.execute(select(Tenant.id))).scalars().all()

        for tenant_id in tenant_ids:
            # 上下文必须在事务开始前绑定，否则 RLS 的 WITH CHECK 不认。
            with tenant_scope(str(tenant_id)):
                async with factory() as session:
                    async with session.begin():
                        result = await session.execute(
                            update(AgentRun)
                            .where(AgentRun.status == RunStatus.RUNNING)
                            .values(
                                status=RunStatus.FAILED,
                                error_category="process_restarted",
                                output_summary="进程重启，该请求已中断。",
                                finished_at=datetime.now(UTC),
                            )
                        )
                        total += result.rowcount or 0
    except Exception:
        logger.warning(
            "清理遗留运行记录失败，跳过。",
            extra={"event": "stale_runs_cleanup_failed"},
            exc_info=True,
        )
        return 0

    if total:
        logger.warning(
            "已将遗留的 RUNNING 运行记录标记为中断。",
            extra={"event": "stale_runs_interrupted", "count": total},
        )

    return total
