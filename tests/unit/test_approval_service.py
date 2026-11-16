# tests/unit/test_approval_service.py
from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import async_session_factory
from app.models.approval import ApprovalRequest, ApprovalStatus, RiskLevel
from app.services.approval_service import (
    ApprovalExpiredError,
    ApprovalParameterMismatchError,
    ApprovalService,
)
from app.tools.gateway import ToolGateway
from app.tools.registry import ToolRegistry
from app.tools.types import (
    ToolCallPlan,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

_counter = itertools.count(1)


def run_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id=f"th-{next(_counter)}",
        run_id="r1",
        request_id="req1",
    )


def build_service(calls: list[dict], *, required_scopes: tuple[str, ...] = ()) -> ApprovalService:
    async def write_handler(ctx, arguments):  # noqa: ANN001
        calls.append({"arguments": arguments, "idempotency_key": ctx.idempotency_key})
        return ToolExecutionResult(
            success=True,
            verified=False,
            external_operation_id="op-1",
            result={"ok": True},
            message="已提交。",
        )

    registry = ToolRegistry()
    gateway = ToolGateway(registry)
    gateway.register_tool(
        ToolDefinition(
            name="create_ticket",
            display_name="创建工单",
            description="测试写工具。",
            kind="python",
            risk_level=RiskLevel.WRITE,
            requires_approval=True,
            required_scopes=required_scopes,
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=write_handler,
        )
    )

    return ApprovalService(
        session_factory=async_session_factory,
        tool_gateway=gateway,
        default_expire_seconds=300,
    )


def plan(context: ToolExecutionContext, title: str = "压力异常") -> ToolCallPlan:
    return ToolCallPlan(
        tool_name="create_ticket",
        display_name="创建工单",
        risk_level=RiskLevel.WRITE,
        arguments={"title": title},
        reason="用户请求创建工单。",
        requires_approval=True,
    )


async def test_parameter_mismatch_blocks_execution():
    calls: list[dict] = []
    service = build_service(calls)
    context = run_context()

    approval = await service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan(context),
        preview={"operation": "创建工单"},
    )

    with pytest.raises(ApprovalParameterMismatchError):
        await service.confirm_and_execute(
            approval_id=approval.id,
            context=context,
            confirmed_parameters={"title": "被篡改的参数"},
        )

    assert calls == []


async def test_expired_approval_blocks_execution():
    calls: list[dict] = []
    service = build_service(calls)
    context = run_context()

    approval = await service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan(context),
        preview={},
    )

    async with async_session_factory() as session:
        async with session.begin():
            row = await session.get(ApprovalRequest, approval.id)
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ApprovalExpiredError):
        await service.confirm_and_execute(
            approval_id=approval.id,
            context=context,
            confirmed_parameters={"title": "压力异常"},
        )

    assert calls == []

    async with async_session_factory() as session:
        row = await session.get(ApprovalRequest, approval.id)
        assert row.status is ApprovalStatus.EXPIRED


async def test_same_approval_executes_only_once():
    calls: list[dict] = []
    service = build_service(calls)
    context = run_context()

    approval = await service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan(context),
        preview={},
    )

    first = await service.confirm_and_execute(
        approval_id=approval.id,
        context=context,
        confirmed_parameters={"title": "压力异常"},
    )
    second = await service.confirm_and_execute(
        approval_id=approval.id,
        context=context,
        confirmed_parameters={"title": "压力异常"},
    )

    assert first.status in {ApprovalStatus.EXECUTED, ApprovalStatus.VERIFIED}
    assert second.already_processed is True
    assert len(calls) == 1, "同一审批不得重复触达外部系统"
    assert calls[0]["idempotency_key"]


async def test_reject_changes_status_and_blocks_execution():
    calls: list[dict] = []
    service = build_service(calls)
    context = run_context()

    approval = await service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan(context),
        preview={},
    )

    rejected = await service.reject(
        approval_id=approval.id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )

    assert rejected.status is ApprovalStatus.REJECTED
    assert calls == []
    assert uuid.UUID(str(approval.id))


async def test_confirmation_preserves_platform_admin_flag():
    """确认执行时必须原样带上发起人的身份位。

    执行时代码会重建一份上下文（补审批号、智能体边界快照）。逐字段抄写时
    `is_platform_admin` 漏了，线上现象是：在对话里发起一切正常、停在审批，
    点确认却返回「当前身份已不具备该操作所需的授权」——
    同一时刻同一个人在控制台点按钮是能改的，所以看着像权限配错了。
    """
    calls: list[dict] = []
    service = build_service(calls, required_scopes=("tools:write",))
    context = ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id="th-admin",
        run_id="r1",
        request_id="req1",
        scopes=(),  # 没有 tools:write，靠平台管理员身份放行
        is_platform_admin=True,
    )

    approval = await service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan(context),
        preview={},
    )

    result = await service.confirm_and_execute(
        approval_id=approval.id,
        context=context,
        confirmed_parameters={"title": "压力异常"},
    )

    assert result.status in {ApprovalStatus.EXECUTED, ApprovalStatus.VERIFIED}
    assert len(calls) == 1, "确认后必须真正执行，而不是被授权层拦掉"
