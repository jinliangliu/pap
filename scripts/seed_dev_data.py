# scripts/seed_dev_data.py
"""初始化开发租户、身份与站点授权。

只在非生产环境使用；生产身份应由 OIDC / 身份网关提供，
租户成员关系由平台管理通道（/api/v1/admin）建立。

多租户要点：
- 租户必须先存在，否则 user_roles / station_grants 的外键会失败；
- 成员关系与站点授权都带 tenant_id，必须在 tenant_scope 内写入，
  否则 ORM 会拒绝（缺少租户上下文的写入一律拒绝）；
- tenant_scope 必须**包在事务最外层**：PostgreSQL 的 `app.tenant_id` 是
  事务开始（begin 事件）时就设置好的，事务开完再改上下文对 RLS 无效。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory, dispose_engine
from app.models.rbac import (
    Role,
    RolePermission,
    StationGrant,
    User,
    UserRole,
)
from app.models.tenant import Tenant, TenantQuota, TenantStatus
from app.tenancy.context import tenant_scope

DEV_ROLE = "dev"
DEV_PERMISSIONS = (
    "tools:read",
    "knowledge.read",
    "knowledge.public",
    "knowledge.internal",
    "station.read",
    "maintenance.ticket.create",
    "observability:read",
)

DEV_TENANT_NAME = "本地开发租户"


async def _ensure_tenant(session, tenant_id: str) -> None:  # noqa: ANN001
    if await session.get(Tenant, tenant_id) is not None:
        return

    session.add(Tenant(id=tenant_id, name=DEV_TENANT_NAME, status=TenantStatus.ACTIVE))
    await session.flush()

    session.add(TenantQuota(tenant_id=tenant_id))
    await session.flush()


async def _ensure_role(session, name: str, permissions: tuple[str, ...]) -> Role:  # noqa: ANN001
    role = await session.scalar(select(Role).where(Role.name == name))

    if role is None:
        role = Role(name=name, description="本地开发角色")
        session.add(role)
        await session.flush()

    existing = {
        row
        for row in await session.scalars(
            select(RolePermission.permission).where(RolePermission.role_id == role.id)
        )
    }

    for permission in permissions:
        if permission not in existing:
            session.add(RolePermission(role_id=role.id, permission=permission))

    await session.flush()
    return role


async def seed(*, tenant_id: str, subject: str, station_id: str) -> None:
    # 上下文必须在事务开始前绑定：RLS 的 app.tenant_id 在 begin 事件里写入。
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                await _ensure_tenant(session, tenant_id)
                role = await _ensure_role(session, DEV_ROLE, DEV_PERMISSIONS)

                user = await session.scalar(
                    select(User).where(User.external_subject == subject)
                )

                if user is None:
                    user = User(external_subject=subject, display_name=subject)
                    session.add(user)
                    await session.flush()

                has_role = await session.scalar(
                    select(UserRole).where(
                        UserRole.tenant_id == tenant_id,
                        UserRole.user_id == user.id,
                        UserRole.role_id == role.id,
                    )
                )

                if has_role is None:
                    session.add(
                        UserRole(tenant_id=tenant_id, user_id=user.id, role_id=role.id)
                    )

                grant = await session.scalar(
                    select(StationGrant).where(
                        StationGrant.tenant_id == tenant_id,
                        StationGrant.user_id == user.id,
                        StationGrant.station_id == station_id,
                    )
                )

                if grant is None:
                    session.add(
                        StationGrant(
                            tenant_id=tenant_id,
                            user_id=user.id,
                            station_id=station_id,
                            can_read=True,
                            can_write=True,
                        )
                    )

    print(
        f"开发身份就绪：tenant={tenant_id}, subject={subject}, station={station_id}"
    )


def main() -> int:
    if settings.is_production:
        print("拒绝在生产环境创建开发身份。", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description="初始化开发租户与身份")
    parser.add_argument(
        "--tenant",
        default=settings.default_tenant_id,
        help="目标租户 ID，默认取 DEFAULT_TENANT_ID",
    )
    parser.add_argument("--subject", default=None, help="外部主体标识，默认取 DEV_SUBJECT")
    parser.add_argument("--station", default="station-001", help="要授权的站点 ID")
    args = parser.parse_args()

    subject = args.subject or settings.dev_subject

    asyncio.run(_run(tenant_id=args.tenant, subject=subject, station_id=args.station))
    return 0


async def _run(*, tenant_id: str, subject: str, station_id: str) -> None:
    try:
        await seed(tenant_id=tenant_id, subject=subject, station_id=station_id)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
