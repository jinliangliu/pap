# app/services/authorization_service.py
"""RBAC、租户成员关系与站点授权。

访问站点资源必须同时满足：
    用户已认证 + 用户启用 + 属于该租户 + 角色含所需 permission
    + 已授予目标 station_id + 写操作要求 can_write

多租户关键点：角色与权限**按租户解析**。同一个账号在 A 租户是管理员，
不会让他在 B 租户也拥有管理员权限——这是把「身份」与「成员资格」分开的原因。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.rbac import Role, RolePermission, StationGrant, User, UserRole
from app.tenancy.context import tenant_scope
from app.tools.types import ToolExecutionContext

PLATFORM_ADMIN_ROLE = "platform_admin"


class AuthorizationError(Exception):
    """当前身份不满足访问条件。"""


async def resolve_user(
    session: AsyncSession,
    *,
    external_subject: str,
) -> User:
    user = await session.scalar(
        select(User).where(User.external_subject == external_subject)
    )

    if user is None or not user.enabled:
        raise AuthorizationError("当前用户不可用或未获授权。")

    return user


async def get_user_roles(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: str,
) -> list[str]:
    """该用户在指定租户内的角色。空列表 = 不是该租户成员。"""
    rows = await session.scalars(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            UserRole.tenant_id == tenant_id,
        )
    )
    return sorted(set(rows))


async def get_user_permissions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: str,
) -> list[str]:
    rows = await session.scalars(
        select(RolePermission.permission)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.tenant_id == tenant_id,
        )
    )
    return sorted(set(rows))


async def get_user_tenant_ids(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[str]:
    """该用户所属的全部租户（成员关系 = 至少有一个租户内角色）。"""
    rows = await session.scalars(
        select(UserRole.tenant_id)
        .where(UserRole.user_id == user_id)
        .distinct()
    )
    return sorted(set(rows))


async def is_platform_admin(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> bool:
    """平台管理员 = 在平台租户里持有 platform_admin 角色。"""
    role = await session.scalar(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            UserRole.tenant_id == settings.platform_tenant_id,
            Role.name == PLATFORM_ADMIN_ROLE,
        )
        .limit(1)
    )
    return role is not None


async def require_permission(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tenant_id: str,
    permission: str,
) -> None:
    permissions = await get_user_permissions(
        session,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    if permission not in permissions:
        raise AuthorizationError("当前用户没有执行该操作的权限。")


async def require_station_access(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    station_id: str,
    write: bool,
) -> None:
    grant = await session.scalar(
        select(StationGrant)
        .where(StationGrant.tenant_id == tenant_id)
        .where(StationGrant.user_id == user_id)
        .where(StationGrant.station_id == station_id)
    )

    allowed = bool(grant and (grant.can_write if write else grant.can_read))

    if not allowed:
        raise AuthorizationError("当前用户没有该站点的访问权限。")


class DbStationAuthorizer:
    """Gateway 使用的站点授权适配器。"""

    async def ensure_access(
        self,
        *,
        context: ToolExecutionContext,
        station_id: str,
        write: bool,
    ) -> None:
        from app.tools.gateway import ToolResourceDeniedError

        try:
            user_id = uuid.UUID(context.user_id)
        except (ValueError, AttributeError) as exc:
            raise ToolResourceDeniedError("当前资源不可访问。") from exc

        async with async_session_factory() as session:
            with tenant_scope(context.tenant_id):
                try:
                    await require_station_access(
                        session,
                        tenant_id=context.tenant_id,
                        user_id=user_id,
                        station_id=station_id,
                        write=write,
                    )
                except AuthorizationError as exc:
                    raise ToolResourceDeniedError("当前资源不可访问。") from exc
