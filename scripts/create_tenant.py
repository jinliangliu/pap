# scripts/create_tenant.py
"""开户脚本：建租户、加成员、可选签发调试 Token。

这是「平台管理员通道」的命令行入口，走的是与
`/api/v1/admin/*` 完全相同的服务调用，因此规则一致：

- 必须显式声明目标租户（`platform_admin_tenant_scope`）；
- 每个写操作都写一条 `ADMIN_ACTION` 审计；
- 不提供读取租户业务数据的任何手段。

用法：
    uv run python -m scripts.create_tenant --tenant acme --name "ACME"
    uv run python -m scripts.create_tenant --tenant acme --member alice --role ops
    uv run python -m scripts.create_tenant --tenant acme --token alice --ttl 3600
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.auth.tokens import encode_access_token
from app.core.config import settings
from app.db.session import async_session_factory, dispose_engine
from app.models.audit import AuditEventType
from app.models.tenant import Tenant
from app.services.audit_service import write_audit_event
from app.services.tenant_service import get_tenant_service
from app.tenancy.context import platform_admin_tenant_scope, tenant_scope

ACTOR = "cli:create_tenant"


async def _audit(session, *, tenant_id: str, operation: str, metadata: dict) -> None:  # noqa: ANN001
    await write_audit_event(
        session,
        event_type=AuditEventType.ADMIN_ACTION,
        tenant_id=tenant_id,
        actor_id=ACTOR,
        resource_type="tenant",
        resource_id=tenant_id,
        message=operation,
        metadata={"operation": operation, "actor_subject": ACTOR, **metadata},
    )


async def _create(tenant_id: str, name: str, daily: int, per_minute: int) -> None:
    with tenant_scope(settings.platform_tenant_id):
        with platform_admin_tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    try:
                        tenant = await get_tenant_service().create_tenant(
                            session,
                            tenant_id=tenant_id,
                            name=name,
                            daily_chat_requests=daily,
                            tool_calls_per_minute=per_minute,
                        )
                    except ValueError as exc:
                        print(f"开户失败：{exc}", file=sys.stderr)
                        raise SystemExit(1) from exc

                    await _audit(
                        session,
                        tenant_id=tenant_id,
                        operation="tenant_created",
                        metadata={"name": name, "source": "cli"},
                    )

    print(f"租户已创建：{tenant.id}（{tenant.name}）")


async def _add_member(tenant_id: str, subject: str, role: str) -> None:
    with tenant_scope(settings.platform_tenant_id):
        with platform_admin_tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    try:
                        user, assigned = await get_tenant_service().add_member(
                            session,
                            tenant_id=tenant_id,
                            subject=subject,
                            role_name=role,
                        )
                    except ValueError as exc:
                        print(f"添加成员失败：{exc}", file=sys.stderr)
                        raise SystemExit(1) from exc

                    await _audit(
                        session,
                        tenant_id=tenant_id,
                        operation="member_upserted",
                        metadata={"subject": subject, "role": assigned.name},
                    )

    print(f"成员已就绪：{user.external_subject} → {tenant_id}/{assigned.name}")


async def _tenant_exists(tenant_id: str) -> bool:
    async with async_session_factory() as session:
        return await session.get(Tenant, tenant_id) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="创建租户、添加成员、签发调试 Token")
    parser.add_argument("--tenant", required=True, help="租户 ID（受限字符集）")
    parser.add_argument("--name", default="", help="租户显示名")
    parser.add_argument("--daily-chat", type=int, default=0, help="每日对话配额，0=平台默认")
    parser.add_argument("--tool-per-minute", type=int, default=0, help="每分钟工具调用上限，0=平台默认")
    parser.add_argument("--member", default="", help="要加入租户的外部主体标识")
    parser.add_argument("--role", default="", help="成员角色名（member 时必填）")
    parser.add_argument("--token", default="", help="为该校验主体签发一个调试 Token 并打印")
    parser.add_argument("--ttl", type=int, default=3600, help="Token 有效期（秒）")
    args = parser.parse_args()

    if args.member and not args.role:
        parser.error("--member 必须同时指定 --role")

    asyncio.run(_run(args))
    return 0


async def _run(args: argparse.Namespace) -> None:
    try:
        if not await _tenant_exists(args.tenant):
            await _create(
                args.tenant,
                args.name or args.tenant,
                args.daily_chat,
                args.tool_per_minute,
            )
        else:
            print(f"租户已存在，跳过创建：{args.tenant}")

        if args.member:
            await _add_member(args.tenant, args.member, args.role)

        if args.token:
            secret = (settings.jwt_secret or "").strip()

            if not secret:
                print(
                    "未配置 JWT_SECRET，无法签发 Token；"
                    "开发环境可改用 AUTH_MODE=dev + X-Authenticated-Subject。",
                    file=sys.stderr,
                )
                return

            token = encode_access_token(
                subject=args.token,
                tenant_id=args.tenant,
                secret=secret,
                expires_in_seconds=args.ttl,
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
            )
            print(token)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
