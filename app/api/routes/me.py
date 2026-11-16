# app/api/routes/me.py
"""当前身份自检与个人访问令牌。

令牌这部分的权限模型很简单：**谁签的谁管**。没有「管理员替别人签发」，
也没有「管理员查看别人的令牌」——那种能力等于给平台管理员一把万能钥匙，
而它的用途（配置外部连接器）完全可以用自己的身份完成。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import Field

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.audit import AuditEventType
from app.schemas.api_token import (
    TokenCreateRequest,
    TokenCreateResponse,
    TokenItem,
    TokenListResponse,
)
from app.schemas.common import APIModel
from app.services import api_token_service
from app.services.api_token_service import TokenLimitError
from app.services.audit_service import write_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


class MeResponse(APIModel):
    user_id: str
    subject: str
    display_name: str

    tenant_id: str
    tenant_name: str
    tenants: list[str] = Field(default_factory=list)

    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    is_platform_admin: bool = False


@router.get("/me", response_model=MeResponse)
async def me(identity: Identity = Depends(get_current_identity)) -> MeResponse:
    """返回当前身份与租户上下文；不返回任何 Token 或密钥。"""
    return MeResponse(
        user_id=identity.user_id,
        subject=identity.subject,
        display_name=identity.display_name,
        tenant_id=identity.tenant_id,
        tenant_name=identity.tenant_name,
        tenants=list(identity.tenants),
        roles=list(identity.roles),
        scopes=list(identity.scopes),
        is_platform_admin=identity.is_platform_admin,
    )


# ---------------- 个人访问令牌 ----------------


def _item(row) -> TokenItem:
    return TokenItem(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        tenant_id=row.tenant_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        active=row.is_active(),
    )


@router.get("/me/tokens", response_model=TokenListResponse)
async def list_tokens(
    identity: Identity = Depends(get_current_identity),
) -> TokenListResponse:
    """列出自己在该租户下的令牌。明文不可能出现在这里——库里只有哈希。"""
    async with async_session_factory() as session:
        rows = await api_token_service.list_tokens(
            session,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            # 撤销过的也列出来：用户需要看到「哪枚已经废了」，
            # 否则会以为它还在某台机器上跑着，从而不敢确定该不该再签一枚。
            include_revoked=True,
        )

        return TokenListResponse(items=[_item(row) for row in rows])


@router.post(
    "/me/tokens",
    response_model=TokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_token(
    payload: TokenCreateRequest,
    identity: Identity = Depends(get_current_identity),
) -> TokenCreateResponse:
    """签发一枚令牌。**明文只在这个响应里出现一次。**"""
    async with async_session_factory() as session:
        async with session.begin():
            try:
                row, raw = await api_token_service.issue_token(
                    session,
                    identity=identity,
                    name=payload.name,
                    expires_in_days=payload.expires_in_days or None,
                )
            except TokenLimitError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc

            await write_audit_event(
                session,
                event_type=AuditEventType.API_TOKEN_ISSUED,
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="api_token",
                resource_id=str(row.id),
                message=f"签发访问令牌：{row.name}",
                metadata={"prefix": row.prefix, "expires_at": str(row.expires_at or "")},
            )

            item = TokenCreateResponse(
                **_item(row).model_dump(),
                token=raw,
            )
            # commit 之后行会过期，再读属性会触发一次已关闭会话上的懒加载。
            # 要留到日志里用的值在这里就取出来。
            token_id, prefix = str(row.id), row.prefix

    logger.info(
        "已签发访问令牌。",
        extra={
            "event": "api_token_issued",
            "token_id": token_id,
            "prefix": prefix,
            "tenant_id": identity.tenant_id,
        },
    )
    return item


@router.delete("/me/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> Response:
    """撤销。下一次调用立即 401，不需要等任何缓存过期。

    重复撤销返回 204 而不是 404：DELETE 的语义是「确保它不能用」，
    第二次达到的状态与第一次相同。只有当这枚令牌**不属于调用者**时才 404。
    """
    async with async_session_factory() as session:
        async with session.begin():
            row = await api_token_service.revoke_token(
                session,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                token_id=token_id,
            )

            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="找不到这枚令牌。",
                )

            await write_audit_event(
                session,
                event_type=AuditEventType.API_TOKEN_REVOKED,
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="api_token",
                resource_id=str(row.id),
                message=f"撤销访问令牌：{row.name}",
                metadata={"prefix": row.prefix},
            )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
