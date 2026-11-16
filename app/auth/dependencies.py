# app/auth/dependencies.py
"""身份与租户归属解析。

三条硬规则：

1. `tenant_id` 只能来自可信身份（JWT 声明 / 身份网关），请求体里的同名字段一律忽略；
2. 请求头 `X-Authenticated-Tenant` 只能作为「切换到哪个组织」的申请，
   服务端必须再校验该用户是不是这个租户的成员，否则 403；
3. 角色与权限按租户解析，绝不复用另一个租户里的角色。

auth_mode：
- dev     : 允许用 X-Authenticated-Subject 指定主体，并自动建立最小开发身份（仅 development）
- gateway : 只接受可信身份网关注入的已认证声明，未知主体不建号
- jwt     : 校验 Bearer Token 签名，从声明里取 sub / tenant_id
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.rbac import Role, RolePermission, User, UserRole
from app.models.tenant import Tenant, TenantStatus
from app.services.authorization_service import (
    AuthorizationError,
    get_user_permissions,
    get_user_roles,
    get_user_tenant_ids,
    is_platform_admin,
)
from app.tenancy.context import (
    TenantIsolationError,
    normalize_tenant_id,
    tenant_scope,
)

logger = logging.getLogger(__name__)

SUBJECT_HEADER = "X-Authenticated-Subject"
TENANT_HEADER = "X-Authenticated-Tenant"

DEV_ROLE_NAME = "dev"
DEV_PERMISSIONS = (
    "tools:read",
    "knowledge.read",
    "knowledge.public",
    "knowledge.internal",
    "station.read",
    "maintenance.ticket.create",
    "observability:read",
    # 连接器凭据。放在 dev 角色里，是为了本地能真正把「接一个外部系统」
    # 走通一遍；连接器的**定义**（端点、启停）仍然要求平台管理员。
    "connector:write",
)


class AuthenticationError(Exception):
    """未认证（401）。"""


class TenantAccessDenied(Exception):
    """已认证，但不属于目标租户（403）。"""


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    subject: str
    display_name: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]

    tenant_name: str = ""
    is_platform_admin: bool = False
    tenants: tuple[str, ...] = ()


async def _ensure_role(
    session: AsyncSession,
    *,
    name: str,
    permissions: tuple[str, ...],
    description: str,
) -> Role:
    role = await session.scalar(select(Role).where(Role.name == name))

    if role is None:
        role = Role(name=name, description=description)
        session.add(role)
        await session.flush()

        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))

    return role


async def _ensure_tenant(session: AsyncSession, *, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)

    if tenant is None:
        tenant = Tenant(id=tenant_id, name=tenant_id, status=TenantStatus.ACTIVE)
        session.add(tenant)
        await session.flush()

    return tenant


async def _provision_dev_identity(
    session: AsyncSession,
    *,
    subject: str,
    tenant_id: str,
) -> User:
    """仅在 development + dev 模式调用：建立最小可用身份。

    生产环境（gateway/jwt）必须先由身份源建号、再分配租户角色，
    否则反代配置失误就能让任意主体自建账号并自选租户。
    """
    with tenant_scope(tenant_id):
        await _ensure_tenant(session, tenant_id=tenant_id)
        role = await _ensure_role(
            session,
            name=DEV_ROLE_NAME,
            permissions=DEV_PERMISSIONS,
            description="本地开发角色",
        )

        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()

        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role_id=role.id))
        await session.flush()

    logger.info(
        "已创建开发身份与租户成员关系。",
        extra={
            "event": "dev_identity_provisioned",
            "subject": subject,
            "tenant_id": tenant_id,
        },
    )
    return user


async def resolve_identity(
    session: AsyncSession,
    *,
    subject: str | None,
    tenant_id: str | None = None,
    token_tenant_id: str | None = None,
) -> Identity:
    """
    解析身份。

    `tenant_id` 是请求头里的切换申请，`token_tenant_id` 是 Token 声明；
    两者都没有时回落到 DEFAULT_TENANT_ID。
    """
    if not subject:
        raise AuthenticationError("未认证用户。")

    user = await session.scalar(
        select(User).where(User.external_subject == subject)
    )

    if user is None:
        if settings.app_env != "development" or settings.auth_mode != "dev":
            logger.warning(
                "拒绝未知身份。",
                extra={
                    "event": "auth_unknown_subject",
                    "subject": subject,
                    "auth_mode": settings.auth_mode,
                },
            )
            raise AuthenticationError("未认证用户。")

        try:
            target = normalize_tenant_id(tenant_id or settings.default_tenant_id)
        except TenantIsolationError as exc:
            raise AuthenticationError("租户标识无效。") from exc

        user = await _provision_dev_identity(
            session,
            subject=subject,
            tenant_id=target,
        )

    if not user.enabled:
        raise AuthenticationError("当前用户已停用。")

    try:
        requested = normalize_tenant_id(
            tenant_id or token_tenant_id or settings.default_tenant_id
        )
    except TenantIsolationError as exc:
        raise TenantAccessDenied("目标租户标识无效。") from exc

    tenant = await session.get(Tenant, requested)

    if tenant is None:
        if settings.app_env == "development" and settings.auth_mode == "dev":
            with tenant_scope(requested):
                tenant = await _ensure_tenant(session, tenant_id=requested)
        else:
            raise TenantAccessDenied("当前用户不属于该租户。")

    if tenant.status is not TenantStatus.ACTIVE:
        raise TenantAccessDenied("该租户已停用。")

    try:
        roles = await get_user_roles(session, user_id=user.id, tenant_id=requested)
        scopes = await get_user_permissions(session, user_id=user.id, tenant_id=requested)
    except AuthorizationError as exc:  # pragma: no cover - 防御性
        raise AuthenticationError("无法解析当前用户权限。") from exc

    if not roles:
        if settings.app_env == "development" and settings.auth_mode == "dev":
            with tenant_scope(requested):
                role = await _ensure_role(
                    session,
                    name=DEV_ROLE_NAME,
                    permissions=DEV_PERMISSIONS,
                    description="本地开发角色",
                )

                exists = await session.scalar(
                    select(UserRole).where(
                        UserRole.tenant_id == requested,
                        UserRole.user_id == user.id,
                        UserRole.role_id == role.id,
                    )
                )

                if exists is None:
                    session.add(
                        UserRole(tenant_id=requested, user_id=user.id, role_id=role.id)
                    )
                    await session.flush()

            roles = await get_user_roles(session, user_id=user.id, tenant_id=requested)
            scopes = await get_user_permissions(
                session, user_id=user.id, tenant_id=requested
            )
        else:
            # 明确区分「没这个人」和「这个人不属于该租户」：
            # 后者是 403，不是 401——身份已经确认，只是没这个租户的授权。
            raise TenantAccessDenied("当前用户不属于该租户。")

    platform_admin = await is_platform_admin(session, user_id=user.id)
    tenants = tuple(await get_user_tenant_ids(session, user_id=user.id))

    return Identity(
        user_id=str(user.id),
        tenant_id=requested,
        subject=user.external_subject,
        display_name=user.display_name,
        roles=tuple(roles),
        scopes=tuple(scopes),
        tenant_name=tenant.name,
        is_platform_admin=platform_admin,
        tenants=tenants,
    )
