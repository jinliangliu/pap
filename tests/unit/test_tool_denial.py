# tests/unit/test_tool_denial.py
"""被拒绝的调用：要有账，也要说清是谁拒的。

守三件事：

1. **拒绝要落审计。** `TOOL_DENIED` 这个事件类型早就定义了，但从来没有代码
   写过它——审计里数不出「这个 Agent 的边界被撞了几次」。
2. **`tool_denied` 不能被 `success=False` 改写成 `tool_failed`。**
   前者是治理信号（该改配置），后者是故障信号（该查代码），归成一类就再也分不开。
3. **四类拒绝各有口径**（scope / role / tenant / agent）。全归成 `tool_rejected`
   的话，运营看不出该改授权还是该改 Agent 边界。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents.nodes import _agent_hidden_tools, _denial_category
from app.models.approval import RiskLevel
from app.models.audit import AuditEventType, AuditLog
from app.tools.gateway import (
    DENIAL_AGENT,
    DENIAL_ROLE,
    DENIAL_SCOPE,
    DENIAL_TENANT,
    ToolGateway,
    ToolPermissionDeniedError,
)
from app.tools.registry import ToolRegistry
from app.tools.types import ToolDefinition, ToolExecutionContext

TENANT = "default"


class _RecordingSink:
    """只记账、不落库的审计适配器，用来断言「拒绝时到底写了什么」。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


def _tool(
    name: str,
    risk: RiskLevel = RiskLevel.READ,
    *,
    scopes: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
) -> ToolDefinition:
    async def _handler(context, arguments):  # pragma: no cover - 拒绝路径不会走到
        raise AssertionError("拒绝路径不应执行 handler")

    return ToolDefinition(
        name=name,
        display_name=name,
        description=name,
        kind="python",
        risk_level=risk,
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        required_scopes=scopes,
        allowed_roles=roles,
    )


def _context(**kwargs) -> ToolExecutionContext:
    base = {
        "tenant_id": TENANT,
        "user_id": "u1",
        "thread_id": "t1",
        "run_id": "r1",
        "request_id": "req1",
        "roles": ("tester",),
        "scopes": ("tools:read",),
    }
    base.update(kwargs)
    return ToolExecutionContext(**base)


def _gateway(tool: ToolDefinition, sink: _RecordingSink) -> ToolGateway:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolGateway(registry, audit_sink=sink)


async def test_agent_boundary_denial_is_audited_with_reason():
    """Agent 边界拦下来的调用，审计里必须能查到「是边界拦的」。"""
    tool = _tool("get_station_snapshot")
    sink = _RecordingSink()
    gateway = _gateway(tool, sink)

    context = _context(
        agent_allowed_tools=("other.tool",),
        agent_name="ops",
        agent_revision=3,
    )

    with pytest.raises(ToolPermissionDeniedError) as caught:
        await gateway.execute_read_only(
            context=context,
            tool_name="get_station_snapshot",
            arguments={"q": "秘密参数值"},
        )

    assert caught.value.reason == DENIAL_AGENT

    event, payload = sink.events[-1]
    assert event == "tool_denied"
    assert payload["denial_reason"] == DENIAL_AGENT
    assert payload["error_code"] == "DENIED_AGENT"
    assert payload["success"] is False
    # 当时是哪个智能体在跑、跑的是哪一版配置：出了问题要能回溯。
    assert payload["agent_name"] == "ops"
    assert payload["agent_revision"] == 3
    # 拒绝发生在参数校验之前，不把参数值写进审计。
    assert "arguments" not in payload


async def test_scope_and_role_denials_are_told_apart():
    """同一句「没权限」，来源不同，该改的东西也不同。"""
    scoped = _tool("station.read.tool", scopes=("station.read",))
    sink = _RecordingSink()
    gateway = _gateway(scoped, sink)

    with pytest.raises(ToolPermissionDeniedError) as caught:
        await gateway.execute_read_only(
            context=_context(scopes=()),
            tool_name="station.read.tool",
            arguments={},
        )
    assert caught.value.reason == DENIAL_SCOPE
    assert sink.events[-1][1]["denial_reason"] == DENIAL_SCOPE

    rolled = _tool("ops.only.tool", roles=("ops",))
    sink2 = _RecordingSink()
    gateway2 = _gateway(rolled, sink2)

    with pytest.raises(ToolPermissionDeniedError) as caught2:
        await gateway2.execute_read_only(
            context=_context(roles=("tester",)),
            tool_name="ops.only.tool",
            arguments={},
        )
    assert caught2.value.reason == DENIAL_ROLE
    assert sink2.events[-1][1]["denial_reason"] == DENIAL_ROLE


async def test_approved_path_also_audits_denial():
    """审批执行入口同样会被边界拦——那条路径的拒绝也不能漏账。"""
    tool = _tool("create_maintenance_ticket", RiskLevel.WRITE)
    sink = _RecordingSink()
    gateway = _gateway(tool, sink)

    with pytest.raises(ToolPermissionDeniedError) as caught:
        await gateway.execute_approved(
            context=_context(agent_can_write=False, agent_name="ops"),
            tool_name="create_maintenance_ticket",
            arguments={},
        )

    assert caught.value.reason == DENIAL_AGENT
    assert sink.events[-1][0] == "tool_denied"


class _NoPolicyService:
    """没有租户策略时的桩：目录只按 scope / 角色 / Agent 边界过滤。"""

    def cached(self, tenant_id: str):  # noqa: ARG002 - 只需要存在
        return None


def test_boundary_hidden_tools_are_computed():
    """被边界裁掉的工具不会有「拒绝」记录，只能靠差值算出来。

    它们的表现是「模型说做不了」，而不是任何异常——所以这是「边界配窄了」
    唯一的信号来源。
    """
    registry = ToolRegistry()
    registry.register(_tool("get_platform_time"))
    registry.register(_tool("create_maintenance_ticket", RiskLevel.WRITE))
    gateway = ToolGateway(registry, tenant_service=_NoPolicyService())

    narrowed = _context(agent_allowed_tools=("get_platform_time",), agent_can_write=True)
    assert _agent_hidden_tools(gateway, narrowed) == ["create_maintenance_ticket"]

    readonly = _context(agent_allowed_tools=None, agent_can_write=False)
    assert _agent_hidden_tools(gateway, readonly) == ["create_maintenance_ticket"]

    # 平台基线：没收窄就早退，不为这个功能扫目录。
    assert _agent_hidden_tools(gateway, _context()) == []


def test_denial_category_covers_every_reason():
    """口径映射要穷举——新增 reason 时忘了扩这里，观测就会静默归错类。"""
    cases = {
        DENIAL_AGENT: "tool_blocked_by_agent",
        DENIAL_ROLE: "tool_blocked_by_role",
        DENIAL_TENANT: "tool_blocked_by_tenant",
        DENIAL_SCOPE: "tool_blocked_by_scope",
    }
    for reason, expected in cases.items():
        assert _denial_category(ToolPermissionDeniedError("x", reason=reason)) == expected

    # 未知 reason 收敛到 scope，不抛异常：审计口径不该让请求失败。
    assert _denial_category(ToolPermissionDeniedError("x", reason="???")) == (
        "tool_blocked_by_scope"
    )


async def test_audit_sink_keeps_denied_event(session, seeded_identity):
    """`tool_denied` 不能被 `success=False` 覆盖成 `tool_failed`。"""
    from app.services.audit_service import DbAuditSink

    sink = DbAuditSink()

    await sink.record(
        "tool_denied",
        {
            "tool_name": "get_station_snapshot",
            "success": False,
            "denial_reason": DENIAL_AGENT,
            "agent_name": "ops",
            "tenant_id": TENANT,
            "user_id": "u1",
        },
    )
    await sink.record(
        "tool_executed",
        {
            "tool_name": "get_station_snapshot",
            "success": False,
            "error_code": "TIMEOUT",
            "tenant_id": TENANT,
            "user_id": "u1",
        },
    )

    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == TENANT)
            .order_by(AuditLog.created_at.asc())
        )
    ).scalars().all()

    by_event = {row.event_type: row for row in rows}
    assert AuditEventType.TOOL_DENIED in by_event
    assert AuditEventType.TOOL_FAILED in by_event

    denied = by_event[AuditEventType.TOOL_DENIED]
    assert denied.metadata_json["denial_reason"] == DENIAL_AGENT
    assert denied.metadata_json["agent_name"] == "ops"
    # 审计页只显示 message 这一列人话文案：没有它，「谁调用了什么」就只剩一行 id。
    assert denied.message == "ops 尝试调用 get_station_snapshot"

    # 没有 agent_name 的事件不编造主语，仍留空。
    assert by_event[AuditEventType.TOOL_FAILED].message is None


async def test_audit_sink_switches_into_the_payload_tenant(monkeypatch):
    """sink 自己开会话，不会继承调用方的 RLS 设置。

    线上就是这里坏的：`audit_logs` 有行级安全策略，事务级 `app.tenant_id`
    没设时插入直接抛 InsufficientPrivilege，而那句 except 把它吞成一行日志。
    结果是审计页上一条工具记录都没有，所有人都以为记了。
    """
    from app.services import audit_service
    from app.tenancy.context import current_tenant

    seen: dict[str, str | None] = {}
    real_factory = audit_service.async_session_factory

    def spy_factory(*args, **kwargs):
        # 会话创建的那一刻，上下文必须已经是 payload 里的租户。
        seen["tenant"] = current_tenant()
        return real_factory(*args, **kwargs)

    monkeypatch.setattr(audit_service, "async_session_factory", spy_factory)

    await audit_service.DbAuditSink().record(
        "tool_executed",
        {
            "tool_name": "get_platform_time",
            "success": True,
            "tenant_id": TENANT,
            "user_id": "u1",
        },
    )

    assert seen["tenant"] == TENANT


async def test_audit_sink_skips_when_tenant_is_unknown():
    """没租户就写不进去，宁可跳过也别硬写出一条被 RLS 拒掉的异常。"""
    from app.db.session import async_session_factory
    from app.services import audit_service

    await audit_service.DbAuditSink().record(
        "tool_executed",
        {"tool_name": "tenantless-probe", "success": True, "user_id": "u1"},
    )

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(AuditLog).where(AuditLog.resource_id == "tenantless-probe")
            )
        ).all()
    assert rows == []
