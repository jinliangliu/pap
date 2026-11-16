# tests/integration/test_approval_run_closure.py
"""审批到达终态时，等待中的运行必须一起收口。

背景：`agent_runs` 只在三处被置成 `WAITING_FOR_APPROVAL`，而审批服务从头到尾
没写过运行状态——单子过期、被拒、执行完，那一轮就永远挂在「等审批」。
看板上于是积着一排点不动的待办，`/observability/agents` 的错误率与 p95 也被
这些永不结束的运行污染（2026-11-13 线上 5 条）。

这里的断言全部落到「运行最后是什么状态、原因写没写、会不会覆盖已经收口的行」，
不断言实现细节——收口走 UPDATE 还是 ORM 都行，写错状态就是错。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.approval import ApprovalRequest, ApprovalStatus, RiskLevel
from app.models.conversation import AgentRun, Conversation, RunStatus
from app.tenancy.context import tenant_scope

TENANT = "default"


async def _make_run(
    *,
    status: RunStatus = RunStatus.WAITING_FOR_APPROVAL,
    approval_id: str | None = None,
    started_at: datetime | None = None,
) -> tuple[uuid.UUID, str]:
    thread_id = f"thread-closure-{uuid.uuid4().hex[:8]}"

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                conversation = Conversation(
                    tenant_id=TENANT,
                    user_id="test-user",
                    thread_id=thread_id,
                    title="审批收口",
                )
                session.add(conversation)
                await session.flush()

                run = AgentRun(
                    conversation_id=conversation.id,
                    tenant_id=TENANT,
                    user_id="test-user",
                    thread_id=thread_id,
                    status=status,
                    approval_id=approval_id,
                    input_summary="给 station-001 的 1 号泵建维护单",
                    started_at=started_at or datetime.now(UTC),
                )
                session.add(run)
                await session.flush()

                return run.id, thread_id


async def _make_approval(
    *,
    thread_id: str,
    run_id: str = "",
    status: ApprovalStatus = ApprovalStatus.PENDING,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    approval_id = uuid.uuid4()

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                session.add(
                    ApprovalRequest(
                        id=approval_id,
                        tenant_id=TENANT,
                        user_id="test-user",
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_name="create_work_order",
                        tool_display_name="创建维护单",
                        risk_level=RiskLevel.WRITE,
                        status=status,
                        reason="测试用",
                        parameters_json={"station": "station-001"},
                        parameter_hash="0" * 64,
                        preview_json={},
                        idempotency_key=f"approval:{uuid.uuid4().hex}",
                        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
                    )
                )

    return approval_id


async def _load_run(run_id: uuid.UUID) -> AgentRun:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            return run


async def test_verified_approval_closes_waiting_run_as_completed():
    from app.services.run_service import close_runs_for_approval

    run_id, thread_id = await _make_run()
    approval_id = await _make_approval(thread_id=thread_id)

    # 老数据的形态：审批单上的 run_id 是空串，只能靠 agent_runs.approval_id 反查。
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    changed = await close_runs_for_approval(
        approval_id=str(approval_id),
        run_id=None,
        tenant_id=TENANT,
        status=ApprovalStatus.VERIFIED,
    )
    assert changed == 1

    run = await _load_run(run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.finished_at is not None
    assert run.error_category is None


async def test_rejected_approval_is_cancelled_not_completed():
    """用户点了取消：这一轮什么也没做成，不能记成完成。"""
    from app.services.run_service import close_runs_for_approval

    run_id, thread_id = await _make_run()
    approval_id = await _make_approval(thread_id=thread_id)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    await close_runs_for_approval(
        approval_id=str(approval_id),
        run_id=None,
        tenant_id=TENANT,
        status=ApprovalStatus.REJECTED,
    )

    run = await _load_run(run_id)
    assert run.status is RunStatus.CANCELLED
    assert run.error_category == "approval_rejected"


async def test_already_closed_run_is_never_overwritten():
    """重复收口不能把一次正常完成的运行改写成别的结论。"""
    from app.services.run_service import close_runs_for_approval

    run_id, thread_id = await _make_run(status=RunStatus.COMPLETED)
    approval_id = await _make_approval(thread_id=thread_id)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)
                run.error_category = "原有结论"
                run.finished_at = datetime.now(UTC)

    changed = await close_runs_for_approval(
        approval_id=str(approval_id),
        run_id=None,
        tenant_id=TENANT,
        status=ApprovalStatus.EXPIRED,
    )

    assert changed == 0
    run = await _load_run(run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.error_category == "原有结论"


async def test_closes_run_matched_by_approval_run_id():
    """新数据的形态：审批单上记着 run_id，运行上反而是空的。"""
    from app.services.run_service import close_runs_for_approval

    run_id, thread_id = await _make_run()
    approval_id = await _make_approval(thread_id=thread_id, run_id=str(run_id))

    changed = await close_runs_for_approval(
        approval_id=str(approval_id),
        run_id=str(run_id),
        tenant_id=TENANT,
        status=ApprovalStatus.FAILED,
    )

    assert changed == 1
    run = await _load_run(run_id)
    assert run.status is RunStatus.FAILED
    assert run.error_category == "approval_failed"


async def test_reconcile_closes_runs_whose_approval_is_terminal():
    """兜底对账要能收拾历史脏行：昨天就过期的单子，运行还挂着。"""
    from app.services.run_service import reconcile_waiting_runs

    run_id, thread_id = await _make_run(
        started_at=datetime.now(UTC) - timedelta(hours=2)
    )
    approval_id = await _make_approval(
        thread_id=thread_id,
        status=ApprovalStatus.EXPIRED,
        expires_at=datetime.now(UTC) - timedelta(hours=2),
    )

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    closed = await reconcile_waiting_runs()
    assert closed >= 1

    run = await _load_run(run_id)
    assert run.status is RunStatus.CANCELLED
    assert run.error_category == "approval_expired"
    assert run.finished_at is not None


async def test_reconcile_leaves_pending_approval_alone():
    """还在有效期内的单子是真的有人在点，不许替他做决定。"""
    from app.services.run_service import reconcile_waiting_runs

    run_id, thread_id = await _make_run(
        started_at=datetime.now(UTC) - timedelta(hours=2)
    )
    approval_id = await _make_approval(thread_id=thread_id)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    await reconcile_waiting_runs()

    run = await _load_run(run_id)
    assert run.status is RunStatus.WAITING_FOR_APPROVAL


async def test_reconcile_respects_grace_period():
    """刚进入等待的运行不动：审批可能还在另一个事务里没提交。"""
    from app.services.run_service import reconcile_waiting_runs

    run_id, thread_id = await _make_run()
    approval_id = await _make_approval(thread_id=thread_id)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    await reconcile_waiting_runs()

    run = await _load_run(run_id)
    assert run.status is RunStatus.WAITING_FOR_APPROVAL


async def test_reconcile_marks_run_without_any_approval_as_failed():
    """连单子都找不到：这轮既没执行也不知道在等什么，记失败让它显形。"""
    from app.services.run_service import reconcile_waiting_runs

    run_id, _thread_id = await _make_run(
        started_at=datetime.now(UTC) - timedelta(hours=2)
    )

    await reconcile_waiting_runs()

    run = await _load_run(run_id)
    assert run.status is RunStatus.FAILED
    assert run.error_category == "approval_missing"


async def test_expiring_approval_closes_its_run():
    """过期任务本身也要收口——这是昨天那 5 条脏行的直接成因。"""
    from app.services.expiry_service import expire_pending_approvals

    run_id, thread_id = await _make_run(
        started_at=datetime.now(UTC) - timedelta(minutes=30)
    )
    approval_id = await _make_approval(
        thread_id=thread_id,
        expires_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    with tenant_scope(TENANT):
        expired = await expire_pending_approvals()
    assert expired >= 1

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval.status is ApprovalStatus.EXPIRED

    run = await _load_run(run_id)
    assert run.status is RunStatus.CANCELLED
    assert run.error_category == "approval_expired"


async def test_pending_approval_run_is_not_touched_by_close():
    """收口函数只认终态；拿 PENDING 调它必须什么都不做。"""
    from app.services.run_service import close_runs_for_approval

    run_id, thread_id = await _make_run()
    approval_id = await _make_approval(thread_id=thread_id)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRun, run_id)
                run.approval_id = str(approval_id)

    changed = await close_runs_for_approval(
        approval_id=str(approval_id),
        run_id=None,
        tenant_id=TENANT,
        status=ApprovalStatus.PENDING,
    )

    assert changed == 0
    run = await _load_run(run_id)
    assert run.status is RunStatus.WAITING_FOR_APPROVAL


async def test_no_waiting_run_is_left_with_terminal_approval():
    """不变式：全库不再存在「等审批」的运行配上已终态的审批单。

    单条断言比前面的例子更容易在将来漏改时提前报错——新加一个终态分支
    却忘了收口，这里就会红。
    """
    from app.services.run_service import reconcile_waiting_runs

    with tenant_scope(TENANT):
        await reconcile_waiting_runs()

        async with async_session_factory() as session:
            stuck = (
                await session.scalars(
                    select(AgentRun.id)
                    .join(
                        ApprovalRequest,
                        ApprovalRequest.id == AgentRun.approval_id,
                    )
                    .where(AgentRun.status == RunStatus.WAITING_FOR_APPROVAL)
                    .where(
                        ApprovalRequest.status.in_(
                            [
                                ApprovalStatus.VERIFIED,
                                ApprovalStatus.EXECUTED,
                                ApprovalStatus.FAILED,
                                ApprovalStatus.REJECTED,
                                ApprovalStatus.EXPIRED,
                                ApprovalStatus.EXECUTION_UNKNOWN,
                                ApprovalStatus.RECOVERY_REQUIRED,
                            ]
                        )
                    )
                )
            ).all()

    assert stuck == []
