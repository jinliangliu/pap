# app/api/routes/permissions.py
"""权限通道：按租户配置「工具能不能用、要不要确认、风险算几级」。

为什么单独一组路由，而不是塞进 `/admin/tenants/{id}/tools/{name}`：
那个接口是「给某个租户开一个工具」的运维动作，一次只能改一个布尔开关；
权限页要做的是**清单式**操作——一眼看到全部工具现在是什么状态、
哪些还没配过，然后逐个调。清单需要一次把所有工具的生效值算好一起返回，
逐条 GET 会变成几十次往返，而且每次都要重算一遍默认动作。

三态策略的语义与执行链的对应关系写在 `app/tools/gateway.py`
（`requires_approval_for`）与 `docs/PERMISSIONS.md`，这里只做校验与落库。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_platform_admin
from app.api.routes.admin import _audit
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.custom_tool import CustomTool
from app.models.tenant import TenantToolPolicy
from app.schemas.permissions import (
    PermissionsResponse,
    PermissionsSummary,
    PlatformPermissionsUpdate,
    ToolPermissionItem,
    ToolPermissionUpdate,
)
from app.services import runtime_settings
from app.services.tenant_service import get_tenant_service
from app.tenancy.context import normalize_tenant_id, platform_admin_tenant_scope
from app.tools.dependencies import get_tool_gateway
from app.tools.gateway import approval_required, risk_override_error, risk_with_override

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/permissions", tags=["permissions"])

_SCOPE = "platform"

# 只列这三类来源。顺序即页面上筛选按钮的顺序：
# 自定义与 MCP 是「接进来的」，内置是「代码里就有的」。
_SOURCES = ("custom", "mcp", "builtin")


def _resolve_tenant(identity: Identity, tenant: str | None) -> str:
    """目标租户。平台管理员可显式指定，其余人只能是自己的租户。"""
    own = normalize_tenant_id(identity.tenant_id)
    if not tenant:
        return own

    target = normalize_tenant_id(tenant)

    if target != own and not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="只能查看自己所在租户的权限配置。")

    return target


async def _policies(session: AsyncSession, tenant_id: str) -> dict[str, TenantToolPolicy]:
    rows = (
        await session.scalars(
            select(TenantToolPolicy).where(TenantToolPolicy.tenant_id == tenant_id)
        )
    ).all()
    return {row.tool_name: row for row in rows}


async def _methods(session: AsyncSession) -> dict[str, str]:
    """自定义工具的 HTTP 方法。库里才有，注册表里只有 schema。"""
    rows = (await session.scalars(select(CustomTool))).all()
    return {row.name: row.method for row in rows}


def _item(
    *,
    tool,
    policy: TenantToolPolicy | None,
    default_action: str,
    method: str | None,
) -> ToolPermissionItem:
    action = policy.action if policy is not None else None
    override = policy.risk_override if policy is not None else None
    effective_risk = risk_with_override(tool.risk_level, override)
    effective_action = action or default_action

    return ToolPermissionItem(
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description or "",
        source=tool.source,
        kind=tool.kind,
        method=method,
        connector_name=tool.connector_name or "",
        base_risk=tool.risk_level,
        effective_risk=effective_risk,
        action=action,
        effective_action=effective_action,
        # 与执行链共用同一份判定，不复刻规则：页面写「需确认」而执行时直接跑，
        # 比不显示更糟。
        requires_approval=approval_required(
            tool=tool, action=effective_action, risk_override=override
        ),
        allowed_roles=list(policy.allowed_roles or []) if policy is not None else [],
        credential_ref=policy.credential_ref if policy is not None else None,
        risk_override=override,
        enabled=tool.enabled,
    )


@router.get("", response_model=PermissionsResponse)
async def get_permissions(
    tenant: str | None = Query(default=None, description="目标租户；平台管理员可跨租户"),
    source: str | None = Query(default=None, description="custom / mcp / builtin，留空返回全部"),
    identity: Identity = Depends(require_platform_admin),
) -> PermissionsResponse:
    gateway = get_tool_gateway()
    target = _resolve_tenant(identity, tenant)
    default_action = runtime_settings.default_tool_action()

    with platform_admin_tenant_scope(target):
        async with async_session_factory() as session:
            policies = await _policies(session, target)
            methods = await _methods(session)

    tools = gateway.list_tools(include_disabled=True)

    if source:
        if source not in _SOURCES:
            raise HTTPException(status_code=400, detail="source 只能是 custom/mcp/builtin。")
        tools = [tool for tool in tools if tool.source == source]

    items = [
        _item(
            tool=tool,
            policy=policies.get(tool.name),
            default_action=default_action,
            method=methods.get(tool.name),
        )
        for tool in tools
    ]

    summary = PermissionsSummary(total=len(items))
    for item in items:
        if item.action is None:
            summary.unconfigured += 1
        if item.effective_action == "allow":
            summary.allow += 1
        elif item.effective_action == "confirm":
            summary.confirm += 1
        else:
            summary.deny += 1

    return PermissionsResponse(
        tenant_id=target,
        default_action=default_action,
        default_action_source=runtime_settings.source(runtime_settings.DEFAULT_TOOL_ACTION),
        mcp_inbound_enabled=runtime_settings.mcp_inbound_enabled(),
        mcp_inbound_source=runtime_settings.source(runtime_settings.MCP_INBOUND_ENABLED),
        tools=items,
        summary=summary,
        sources=list(_SOURCES),
    )


@router.put("/tools/{tool_name}", response_model=ToolPermissionItem)
async def set_tool_permission(
    tool_name: str,
    payload: ToolPermissionUpdate,
    tenant: str | None = Query(default=None),
    identity: Identity = Depends(require_platform_admin),
) -> ToolPermissionItem:
    """保存一个工具的策略。**立刻生效**（租户策略缓存当场失效）。"""
    gateway = get_tool_gateway()

    try:
        tool = gateway.get_tool(tool_name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="工具未注册。") from exc

    _validate_risk(tool=tool, override=payload.risk_override)

    target = _resolve_tenant(identity, tenant)

    with platform_admin_tenant_scope(target):
        async with async_session_factory() as session:
            async with session.begin():
                try:
                    policy = await get_tenant_service().set_tool_policy(
                        session,
                        tenant_id=target,
                        tool_name=tool_name,
                        action=payload.action,
                        allowed_roles=payload.allowed_roles,
                        credential_ref=payload.credential_ref,
                        risk_override=payload.risk_override,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=target,
                    operation="tool_policy_updated",
                    resource_type="tenant_tool_policy",
                    resource_id=tool_name,
                    metadata={
                        "action": payload.action,
                        "risk_override": payload.risk_override,
                        "allowed_roles": payload.allowed_roles,
                        "credential_ref": payload.credential_ref,
                    },
                )

            # 审计已提交，再读一次策略用于回显（session 里的行可能已被删除）。
            policies = await _policies(session, target)
            method = (await _methods(session)).get(tool_name)

    return _item(
        tool=tool,
        policy=policies.get(tool_name) if policy is not None else None,
        default_action=runtime_settings.default_tool_action(),
        method=method,
    )


@router.delete("/tools/{tool_name}", response_model=ToolPermissionItem)
async def reset_tool_permission(
    tool_name: str,
    tenant: str | None = Query(default=None),
    identity: Identity = Depends(require_platform_admin),
) -> ToolPermissionItem:
    """恢复为未配置：删掉这一行，让工具重新跟随平台默认动作。"""
    return await set_tool_permission(
        tool_name=tool_name,
        payload=ToolPermissionUpdate(action=None),
        tenant=tenant,
        identity=identity,
    )


@router.put("/platform", response_model=PermissionsResponse)
async def update_platform_permissions(
    payload: PlatformPermissionsUpdate,
    tenant: str | None = Query(default=None),
    identity: Identity = Depends(require_platform_admin),
) -> PermissionsResponse:
    """改两项平台级策略。

    这两项不分租户：它们是「这个进程怎么对待没配过的工具」和
    「对外通道开不开」，按租户分叉会出现同一个工具体对不同租户有不同
    默认值，而默认值的意义恰恰是「不去逐个配置也有一套可预期行为」。
    """
    if payload.default_action not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="默认动作只能是 allow 或 deny。")

    previous_action = runtime_settings.default_tool_action()
    previous_inbound = runtime_settings.mcp_inbound_enabled()

    try:
        await runtime_settings.set_default_tool_action(
            payload.default_action, actor=identity.subject
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await runtime_settings.set_mcp_inbound_enabled(
        payload.mcp_inbound_enabled, actor=identity.subject
    )

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_SCOPE,
                    operation="permission_platform_updated",
                    resource_type="runtime_settings",
                    resource_id="permissions",
                    metadata={
                        "default_action": payload.default_action,
                        "default_action_previous": previous_action,
                        "mcp_inbound_enabled": payload.mcp_inbound_enabled,
                        "mcp_inbound_previous": previous_inbound,
                    },
                )

    return await get_permissions(tenant=tenant, source=None, identity=identity)


def _validate_risk(*, tool, override: str | None) -> None:
    """风险覆盖只能上调。

    把写入类工具标成「读取」等于给它开一条免审批直通：调用不再弹卡片、
    直接执行，而从返回值、审计与页面上都看不出这次调用本该有人确认。
    判定本身在 `gateway.risk_override_error`，与另一个写策略的入口共用。
    """
    reason = risk_override_error(tool, override)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
