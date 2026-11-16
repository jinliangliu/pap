# app/api/dependencies.py
"""HTTP 身份依赖。

请求进来后先完成三件事，之后所有下游代码都只读上下文：

1. 认证：按 AUTH_MODE 从 Bearer Token 或可信网关头取主体；
2. 归属：确认该用户属于目标租户（请求头只能申请，不能决定）；
3. 绑定：把租户写入 ContextVar，ORM 自动过滤与 RLS 都依赖它。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, status

from app.auth.dependencies import (
    SUBJECT_HEADER,
    TENANT_HEADER,
    AuthenticationError,
    Identity,
    TenantAccessDenied,
    resolve_identity,
)
from app.auth.tokens import TokenError, TokenExpired, decode_access_token
from app.core.config import settings
from app.db.session import async_session_factory
from app.tenancy.context import bind_tenant, unbind_tenant


def _bearer_token(authorization: str | None) -> str:
    raw = (authorization or "").strip()

    if not raw.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证用户：缺少 Bearer Token。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return raw.split(" ", 1)[1].strip()


def _authenticate(
    *,
    subject_header: str | None,
    authorization: str | None,
) -> tuple[str, str | None]:
    """返回 (subject, token 声明的 tenant_id)。"""
    if settings.auth_mode == "jwt":
        if not (settings.jwt_secret or "").strip():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="身份服务未配置完成。",
            )

        token = _bearer_token(authorization)

        try:
            claims = decode_access_token(token)
        except TokenExpired as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录状态已过期，请重新登录。",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="身份令牌无效。",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        return claims.subject, claims.tenant_id

    if settings.trusts_gateway_identity:
        if not subject_header:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未认证用户：缺少可信网关注入的身份信息。",
            )

        return subject_header, None

    # dev 模式：允许缺省，回落到具名开发主体，便于本地调试。
    # production 下由 Settings 启动校验直接拒绝该模式。
    return subject_header or settings.dev_subject, None


async def get_current_identity(
    x_authenticated_subject: str | None = Header(default=None, alias=SUBJECT_HEADER),
    x_authenticated_tenant: str | None = Header(default=None, alias=TENANT_HEADER),
    authorization: str | None = Header(default=None),
) -> AsyncIterator[Identity]:
    """
    解析并绑定当前身份。

    认证用独立会话：请求级会话的第一条语句必须在租户上下文确定之后执行，
    否则 PostgreSQL 的事务级 `app.tenant_id` 会来不及生效。
    """
    subject, token_tenant = _authenticate(
        subject_header=x_authenticated_subject,
        authorization=authorization,
    )

    requested_tenant = x_authenticated_tenant if settings.tenant_header_override_enabled else None

    async with async_session_factory() as session:
        try:
            identity = await resolve_identity(
                session,
                subject=subject,
                tenant_id=requested_tenant,
                token_tenant_id=token_tenant,
            )
        except TenantAccessDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        finally:
            # 认证过程只读；显式回滚，避免把未提交事务带进连接池。
            await session.rollback()

    token = bind_tenant(identity.tenant_id)

    try:
        yield identity
    finally:
        unbind_tenant(token)


async def require_platform_admin(
    identity: Identity = Depends(get_current_identity),
) -> AsyncIterator[Identity]:
    """平台管理通道：只有平台租户里的 platform_admin 能进。

    进入后上下文固定为平台租户，具体操作再由 `platform_admin_tenant_scope(target)`
    收窄到目标租户——两步都显式出现，便于审计与代码审查。
    """
    if not identity.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要平台管理员权限。",
        )

    token = bind_tenant(settings.platform_tenant_id)

    try:
        yield identity
    finally:
        unbind_tenant(token)
