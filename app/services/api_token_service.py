# app/services/api_token_service.py
"""个人访问令牌的签发、撤销与校验。

## 明文只出现一次

签发接口回一次明文，之后任何接口都不再返回它——库里只有 sha256。
「忘了就重新签一枚」是刻意的产品行为：可回看的凭据一定会被复制到
聊天记录、截图和脚本里，那时候它就不再是凭据了。

## 校验分两步，第二步在身份层

`resolve_token` 只负责「这枚令牌有效吗、属于谁」；主体是否停用、
是否还在该租户里、有哪些角色，全部交给 `resolve_identity`。
合并成一步的后果是权限判定出现第二份实现，两份迟早不一致。

## `last_used_at` 是尽力而为

它在鉴权热路径上，每次调用都写一次库不划算，失败也不该导致鉴权失败。
所以：超过一分钟才写一次，异常吞掉只记日志。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Identity, resolve_identity
from app.models.api_token import ApiToken
from app.models.rbac import User

logger = logging.getLogger(__name__)

# 令牌前缀。带上前缀是为了让「这是 PAP 的令牌」在日志和截图里一眼可辨，
# 也方便泄露扫描规则认出它。
TOKEN_PREFIX = "pap_"
# 32 字节随机 = 256 bit。没有可枚举空间，这就是不加 pepper 的全部理由。
TOKEN_BYTES = 32
# 列表页展示的前缀长度：够认出是哪一枚，又远远不足以还原令牌。
DISPLAY_PREFIX_CHARS = 12

# 单个用户同时有效的令牌上限。令牌不会被主动清理，
# 没有上限时「签过 40 枚、每枚都能用」是常态，而用户以为旧的那几枚早废了。
MAX_ACTIVE_TOKENS = 20

DEFAULT_TTL_DAYS = 90


class TokenLimitError(Exception):
    """有效令牌数量超过上限。"""


def hash_token(raw: str) -> str:
    """令牌 → 存储用哈希。恒定算法：签发与校验必须用同一份实现。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_uuid(value) -> uuid.UUID | None:
    """把字符串转成 UUID。

    主键是 `Uuid` 列，SQLAlchemy 会直接对它调 `.hex`：传字符串进去抛的是
    `AttributeError` 而不是「找不到」。这类错误在路由层被 FastAPI 的类型注解
    挡住了（`token_id: uuid.UUID`），但脚本、内部调用没有那层保护。
    """
    if isinstance(value, uuid.UUID):
        return value

    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _generate() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


async def issue_token(
    session: AsyncSession,
    *,
    identity: Identity,
    name: str,
    expires_in_days: int | None = DEFAULT_TTL_DAYS,
) -> tuple[ApiToken, str]:
    """签发一枚令牌，返回 `(行, 明文)`。明文此后不再可得。"""
    label = (name or "").strip() or "未命名令牌"

    active = await count_active_tokens(
        session, user_id=identity.user_id, tenant_id=identity.tenant_id
    )

    if active >= MAX_ACTIVE_TOKENS:
        raise TokenLimitError(
            f"已有 {active} 枚有效令牌（上限 {MAX_ACTIVE_TOKENS}）。"
            "先撤销不用的，再签发新的。"
        )

    raw = _generate()
    expires_at = None if expires_in_days is None else _now() + datetime.timedelta(
        days=expires_in_days
    )

    row = ApiToken(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        name=label,
        token_hash=hash_token(raw),
        prefix=raw[:DISPLAY_PREFIX_CHARS],
        expires_at=expires_at,
    )

    session.add(row)
    await session.flush()
    return row, raw


async def count_active_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
) -> int:
    now = _now()
    rows = await session.scalars(
        select(ApiToken).where(
            ApiToken.user_id == user_id,
            ApiToken.tenant_id == tenant_id,
            ApiToken.revoked_at.is_(None),
        )
    )

    # 过期判定在 Python 里做：sqlite 与 PG 对「带时区列和当前时间比较」
    # 的处理不一致，写成 SQL 会在测试里过、在线上少算或多算一批。
    return sum(1 for row in rows if row.is_active(now))


async def list_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
    include_revoked: bool = False,
) -> list[ApiToken]:
    """列出某人自己的令牌。**必须带 user_id**：这张表没有 RLS（见模型注释）。"""
    stmt = select(ApiToken).where(
        ApiToken.user_id == user_id,
        ApiToken.tenant_id == tenant_id,
    )

    if not include_revoked:
        stmt = stmt.where(ApiToken.revoked_at.is_(None))

    stmt = stmt.order_by(ApiToken.created_at.desc())
    return list(await session.scalars(stmt))


async def revoke_token(
    session: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
    token_id,
) -> ApiToken | None:
    """撤销。打标记不删行——删了以后审计里的 actor 就对不回一张令牌了。"""
    token_pk = _as_uuid(token_id)

    if token_pk is None:
        return None

    row = await session.scalar(
        select(ApiToken).where(
            ApiToken.id == token_pk,
            ApiToken.user_id == user_id,
            ApiToken.tenant_id == tenant_id,
        )
    )

    if row is None:
        return None

    if row.revoked_at is None:
        row.revoked_at = _now()
        await session.flush()

    return row


async def find_by_raw(session: AsyncSession, raw: str) -> ApiToken | None:
    """按明文哈希找行。找不到或已失效一律返回 None——对外不区分原因。"""
    token = (raw or "").strip()

    if not token:
        return None

    row = await session.scalar(
        select(ApiToken).where(ApiToken.token_hash == hash_token(token))
    )

    if row is None:
        return None

    if not row.is_active(_now()):
        return None

    return row


async def resolve_token(session: AsyncSession, raw: str) -> Identity | None:
    """令牌 → 身份。无效、过期、撤销、主体停用、不在该租户，一律 None。"""
    row = await find_by_raw(session, raw)

    if row is None:
        return None

    # `users.id` 是 Uuid 列，而令牌行上的 user_id 按全库惯例存成字符串。
    # 直接传字符串进去，SQLAlchemy 会对它调 `.hex` 并抛 AttributeError——
    # 那个异常在这里会被当成「令牌无效」，表现是「令牌明明有效却一直 401」。
    try:
        user_pk = uuid.UUID(str(row.user_id))
    except (ValueError, AttributeError):
        logger.warning(
            "令牌上的 user_id 不是合法标识。",
            extra={"event": "api_token_bad_user_id", "token_id": str(row.id)},
        )
        return None

    user = await session.get(User, user_pk)

    if user is None:
        logger.warning(
            "令牌对应的用户已不存在。",
            extra={"event": "api_token_orphan", "token_id": str(row.id)},
        )
        return None

    # 身份解析会再查一遍角色与租户归属：撤了角色、踢出组织之后
    # 令牌立刻失效，不需要等它过期。
    return await resolve_identity(
        session,
        subject=user.external_subject,
        tenant_id=row.tenant_id,
    )


async def touch_last_used(
    session: AsyncSession,
    *,
    token_id,
    min_interval_seconds: int = 60,
) -> None:
    """记录最近使用时间。失败不应影响鉴权，调用方自己吞异常。"""
    token_pk = _as_uuid(token_id)

    if token_pk is None:
        return

    row = await session.get(ApiToken, token_pk)

    if row is None:
        return

    now = _now()
    last = row.last_used_at

    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=datetime.timezone.utc)
        if (now - last).total_seconds() < min_interval_seconds:
            return

    row.last_used_at = now
    await session.flush()


def token_matches_static(raw: str, configured: str | None) -> bool:
    """与静态兜底令牌比较。恒定时间比较，避免逐字节试探。"""
    expected = (configured or "").strip()

    if not expected or not raw:
        return False

    return hmac.compare_digest(raw.encode("utf-8"), expected.encode("utf-8"))
