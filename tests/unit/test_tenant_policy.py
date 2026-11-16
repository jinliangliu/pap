# tests/unit/test_tenant_policy.py
"""租户级工具策略与凭据引用。

网关的判定顺序是：注册 → 启用 → Schema → 角色/Scope → 租户策略 → 站点授权 → 限流。
租户策略是「平台允许」之外的第二道：同一套平台能力，不同租户可以给不同的人、用不同的凭据。
"""

from __future__ import annotations

import pytest

from app.models.approval import RiskLevel
from app.services.tenant_service import TenantService, TenantSettings, ToolPolicy
from app.tools.gateway import (
    ToolDisabledError,
    ToolGateway,
    ToolPermissionDeniedError,
)
from app.tools.registry import ToolRegistry
from app.tools.types import ToolDefinition, ToolExecutionContext, ToolExecutionResult

TENANT = "tenant-a"


class StubTenantService:
    """只实现网关用到的三个方法，避免为单测拉起数据库。"""

    def __init__(self, settings_: TenantSettings) -> None:
        self._settings = settings_
        self.credentials: dict[str, str] = {}

    def cached(self, tenant_id: str) -> TenantSettings | None:
        return self._settings

    async def settings_for(self, tenant_id: str, *, force: bool = False) -> TenantSettings:
        return self._settings

    def resolve_credential(self, ref: str | None) -> str | None:
        return self.credentials.get(ref) if ref else None


def _context(*, roles: tuple[str, ...] = ("tester",)) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=TENANT,
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        scopes=("tools:read",),
        roles=roles,
    )


def _tool(handler=None) -> ToolDefinition:  # noqa: ANN001
    async def default_handler(context, arguments):  # noqa: ANN001
        return ToolExecutionResult(
            success=True,
            result={
                "echo": arguments,
                "credential": context.credential,
                "tenant": context.tenant_id,
            },
        )

    return ToolDefinition(
        name="echo",
        display_name="回声",
        description="回显参数。",
        kind="python",
        risk_level=RiskLevel.READ,
        required_scopes=("tools:read",),
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler or default_handler,
    )


def _gateway(policy: ToolPolicy | None) -> tuple[ToolGateway, StubTenantService]:
    tools = {_tool().name: policy} if policy is not None else {}
    stub = StubTenantService(TenantSettings(tenant_id=TENANT, tools=tools))
    gateway = ToolGateway(ToolRegistry(), tenant_service=stub)  # type: ignore[arg-type]
    gateway.register_tool(_tool())
    return gateway, stub


async def test_tool_allowed_when_tenant_has_no_policy():
    gateway, _ = _gateway(None)

    result = await gateway.execute_read_only(
        context=_context(),
        tool_name="echo",
        arguments={"value": "x"},
    )

    assert result.success is True
    assert result.result["tenant"] == TENANT


async def test_tenant_can_disable_tool():
    gateway, _ = _gateway(ToolPolicy(action="deny"))

    with pytest.raises(ToolDisabledError):
        await gateway.execute_read_only(
            context=_context(),
            tool_name="echo",
            arguments={"value": "x"},
        )


async def test_tenant_can_restrict_tool_to_roles():
    gateway, _ = _gateway(ToolPolicy(action="allow", allowed_roles=("ops",)))

    with pytest.raises(ToolPermissionDeniedError):
        await gateway.execute_read_only(
            context=_context(roles=("tester",)),
            tool_name="echo",
            arguments={"value": "x"},
        )

    allowed = await gateway.execute_read_only(
        context=_context(roles=("ops",)),
        tool_name="echo",
        arguments={"value": "x"},
    )

    assert allowed.success is True


async def test_credential_is_resolved_per_tenant_into_context():
    """凭据来自租户策略的引用，只存在于本次调用的上下文中。"""
    gateway, stub = _gateway(
        ToolPolicy(action="allow", credential_ref="STATION_TOKEN_TENANT_A")
    )
    stub.credentials["STATION_TOKEN_TENANT_A"] = "secret-a"

    result = await gateway.execute_read_only(
        context=_context(),
        tool_name="echo",
        arguments={"value": "x"},
    )

    assert result.result["credential"] == "secret-a"


async def test_missing_credential_does_not_fail_the_call():
    """凭据没配好时按「无凭据」继续，由下游接口决定是否拒绝。

    静默降级成别的租户凭据才是真正的灾难，这里必须保持 None。
    """
    gateway, _ = _gateway(ToolPolicy(action="allow", credential_ref="NOT_CONFIGURED"))

    result = await gateway.execute_read_only(
        context=_context(),
        tool_name="echo",
        arguments={"value": "x"},
    )

    assert result.success is True
    assert result.result["credential"] is None


def test_credential_ref_must_be_an_env_var_name(monkeypatch):
    """引用只能是环境变量名：密钥本身绝不入库，也不做任何插值。"""
    monkeypatch.setenv("STATION_TOKEN_A", "secret-a")

    assert TenantService.resolve_credential("STATION_TOKEN_A") == "secret-a"
    assert TenantService.resolve_credential("") is None
    assert TenantService.resolve_credential(None) is None

    for bad in ("${STATION_TOKEN_A}", "station_token_a", "A/b", "A B", "1ABC"):
        assert TenantService.resolve_credential(bad) is None


def test_disabled_tenant_tool_is_hidden_from_catalog():
    gateway, _ = _gateway(ToolPolicy(action="deny"))

    visible = gateway.list_available_for_context(context=_context())

    assert [view.tool_name for view in visible] == []
