# tests/unit/test_permission_policy.py
"""权限策略的三态语义与风险覆盖。

这里盯的是「页面上写的那句话，执行时是不是真的这样」：

- `confirm` 能不能让一个只读工具也必须先审批；
- `deny` 是不是既看不见也调不动；
- 风险覆盖能不能把写入工具「降级」成免审批（答案是不能）。

这些判断在两处被用到（执行前的 `requires_approval_for`、权限页的回显），
所以断言都打在 `approval_required` 这个唯一实现上。
"""

from __future__ import annotations

import pytest

from app.models.approval import RiskLevel
from app.services.tenant_service import TenantSettings, ToolPolicy
from app.tools.gateway import (
    ToolApprovalRequiredError,
    ToolGateway,
    approval_required,
    risk_with_override,
)
from app.tools.registry import ToolRegistry
from app.tools.types import ToolDefinition, ToolExecutionContext, ToolExecutionResult

TENANT = "tenant-a"


class StubTenantService:
    def __init__(self, settings_: TenantSettings) -> None:
        self._settings = settings_

    def cached(self, tenant_id: str) -> TenantSettings | None:
        return self._settings

    async def settings_for(self, tenant_id: str, *, force: bool = False) -> TenantSettings:
        return self._settings

    def resolve_credential(self, ref: str | None) -> str | None:
        return None


def _tool(risk: RiskLevel = RiskLevel.READ) -> ToolDefinition:
    async def handler(context, arguments):  # noqa: ANN001
        return ToolExecutionResult(success=True, result={"ok": True})

    return ToolDefinition(
        name="probe",
        display_name="探针",
        description="测试用。",
        kind="python",
        risk_level=risk,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=TENANT,
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        roles=("tester",),
    )


def _gateway(policy: ToolPolicy | None, *, risk: RiskLevel = RiskLevel.READ) -> ToolGateway:
    tools = {"probe": policy} if policy is not None else {}
    stub = StubTenantService(TenantSettings(tenant_id=TENANT, tools=tools))
    gateway = ToolGateway(ToolRegistry(), tenant_service=stub)  # type: ignore[arg-type]
    gateway.register_tool(_tool(risk))
    return gateway


def test_approval_required_by_action_and_risk():
    read_tool = _tool(RiskLevel.READ)
    write_tool = _tool(RiskLevel.WRITE)

    # 允许：只读直接跑，写入仍受兜底约束。
    assert approval_required(tool=read_tool, action="allow") is False
    assert approval_required(tool=write_tool, action="allow") is True

    # 需确认：连只读也要审批。
    assert approval_required(tool=read_tool, action="confirm") is True
    assert approval_required(tool=write_tool, action="confirm") is True

    # 拒绝：不审批，因为根本不会执行（由权限校验拦下）。
    assert approval_required(tool=read_tool, action="deny") is False


def test_risk_override_only_escalates():
    """写入不能被「覆盖成读取」，读取可以被收紧成写入。"""
    assert risk_with_override(RiskLevel.READ, "write") is RiskLevel.WRITE
    assert risk_with_override(RiskLevel.WRITE, "read") is RiskLevel.WRITE
    assert risk_with_override(RiskLevel.DESTRUCTIVE, "read") is RiskLevel.DESTRUCTIVE
    assert risk_with_override(RiskLevel.READ, None) is RiskLevel.READ

    # 上调之后，原本免审批的只读工具也要走审批。
    assert (
        approval_required(tool=_tool(RiskLevel.READ), action="allow", risk_override="write")
        is True
    )


def test_default_action_deny_only_lets_explicit_rows_through():
    """平台默认改成拒绝后，「未配置」与「显式允许」必须分开。"""
    empty = TenantSettings(tenant_id=TENANT)
    assert empty.allow_tool("probe", ("tester",), default_action="allow") is True
    assert empty.allow_tool("probe", ("tester",), default_action="deny") is False

    allowed = TenantSettings(
        tenant_id=TENANT,
        tools={"probe": ToolPolicy(action="allow")},
    )
    assert allowed.allow_tool("probe", ("tester",), default_action="deny") is True

    confirm = TenantSettings(
        tenant_id=TENANT,
        tools={"probe": ToolPolicy(action="confirm")},
    )
    # 需确认的工具仍然可见：看不见就没法调用，也就没法弹出审批。
    assert confirm.allow_tool("probe", ("tester",), default_action="deny") is True

    denied = TenantSettings(
        tenant_id=TENANT,
        tools={"probe": ToolPolicy(action="deny")},
    )
    assert denied.allow_tool("probe", ("tester",), default_action="allow") is False


async def test_confirm_policy_forces_approval_even_for_read_tool():
    gateway = _gateway(ToolPolicy(action="confirm"))
    context = _context()

    assert gateway.requires_approval_for(tool=gateway.get_tool("probe"), context=context) is True

    with pytest.raises(ToolApprovalRequiredError):
        await gateway.execute_read_only(
            context=context,
            tool_name="probe",
            arguments={"value": "x"},
        )


async def test_deny_policy_hides_tool_from_catalog():
    gateway = _gateway(ToolPolicy(action="deny"))

    assert gateway.list_available_for_context(context=_context()) == []


def test_confirm_policy_shows_in_catalog_as_requires_approval():
    """目录里要带着「需审批」这个事实，模型才会预期到会停一下。"""
    gateway = _gateway(ToolPolicy(action="confirm"))

    views = gateway.list_available_for_context(context=_context())

    assert [v.tool_name for v in views] == ["probe"]
    assert views[0].requires_approval is True
