# app/mcp_server/auth.py
"""入向 MCP 通道的凭据校验。

与 `app/api/dependencies.py` 的分工：那一层从**网关注入的请求头**取身份，
这一层从 **Bearer 令牌**取身份。两条路最后都落到同一个 `Identity`，
授权仍然一律以数据库为准（令牌里不带角色，也不该带——撤角色要等令牌过期
才能生效，那不是「撤销」）。

两级凭据：

1. **个人访问令牌（PAT，`api_tokens`）** —— 正常路径。调用方是谁，
   审计里就是谁，审批也有归属。
2. **静态兜底令牌（`MCP_INBOUND_TOKEN`）** —— 给单机自测和「表还没建好」
   的场景用，对应一个固定主体。它不会被悄悄关掉，但也不该长期发放：
   所有调用都记在同一个人头上。

一条硬约束：**主体只能来自凭据，绝不来自请求头。**
带令牌的调用方如果能用 `X-Authenticated-Subject` 指定身份，
那这张令牌就等于一把万能钥匙，租户隔离当场失效。

## 为什么身份在门槛里解析、在工具里读

门槛（`BearerGate`）解析一次身份并放进 `scope["state"]`，工具从
`ctx.request_context.request.state` 直接取。理由不是省一次查询，而是
**无效令牌必须拿到 401**：如果只在工具里校验，一个无效令牌会先建立会话、
再收到一个 JSON-RPC 错误，客户端看到的是「工具调用失败」，而不是「凭据不对」。
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.auth.dependencies import (
    AuthenticationError,
    Identity,
    TenantAccessDenied,
    resolve_identity,
)
from app.core.config import settings
from app.db.session import async_session_factory
from app.services import api_token_service

logger = logging.getLogger(__name__)

REALM = 'Bearer realm="pap-mcp"'

# 门槛解析出的身份放在这个键下。名字带前缀，避免和中间件里的通用键撞。
STATE_KEY = "pap_identity"


def _bearer_value(authorization: str | None) -> str | None:
    """取 Bearer 后面的值。方案名大小写不敏感，值本身不动。"""
    if not authorization:
        return None

    scheme, _, value = authorization.partition(" ")

    if scheme.strip().lower() != "bearer":
        return None

    token = value.strip()
    return token or None


async def _static_identity(raw: str) -> Identity | None:
    """静态兜底令牌对应的固定主体。

    主体与租户都取自配置。没配令牌就直接返回 None——fail-closed，
    绝不回落到「默认租户的默认用户」。
    """
    if not api_token_service.token_matches_static(raw, settings.mcp_inbound_token):
        return None

    async with async_session_factory() as session:
        try:
            return await resolve_identity(
                session,
                subject=settings.mcp_inbound_subject,
                tenant_id=settings.mcp_inbound_tenant,
            )
        except (AuthenticationError, TenantAccessDenied):
            # 主体不存在、已停用、不属于该租户：凭据本身没错，人不能用了。
            # 必须收敛成 None（401），不能把异常抛出去变成 500——
            # 那样调用方会以为「服务挂了」而不是「凭据该换了」。
            logger.warning(
                "静态令牌对应的主体不可用。",
                extra={
                    "event": "mcp_static_subject_unusable",
                    "subject": settings.mcp_inbound_subject,
                },
            )
            return None
        finally:
            # 解析过程只读，显式回滚，别把未提交事务带回连接池。
            await session.rollback()


async def _touch(token_id) -> None:
    """记录 PAT 最近使用时间。失败不影响鉴权。"""
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await api_token_service.touch_last_used(session, token_id=token_id)
    except Exception:  # noqa: BLE001 - 记账失败不该让一次合法调用失败
        logger.warning(
            "更新令牌使用时间失败。",
            extra={"event": "api_token_touch_failed", "token_id": str(token_id)},
        )


async def identity_for_token(raw: str) -> Identity | None:
    """令牌 → 身份。无效返回 None，**不区分**不存在 / 过期 / 已撤销。

    对外区分这三种原因，等于给爆破者一个「这枚存在但过期了」的信号。
    """
    if not raw:
        return None

    static = await _static_identity(raw)
    if static is not None:
        return static

    token_id = None
    identity = None

    async with async_session_factory() as session:
        try:
            row = await api_token_service.find_by_raw(session, raw)

            if row is None:
                return None

            # 先把 id 取出来：会话结束（rollback）之后 ORM 行会过期，
            # 再读属性就是一次已关闭会话上的懒加载。
            token_id = row.id
            identity = await api_token_service.resolve_token(session, raw)
        except (AuthenticationError, TenantAccessDenied):
            # 令牌有效，但人被停用或已不在该租户。同上：收敛成 401。
            logger.warning(
                "令牌对应的身份不可用。",
                extra={"event": "mcp_token_subject_unusable"},
            )
            return None
        finally:
            await session.rollback()

    if identity is None:
        return None

    # 身份解析成功才算「用过」：令牌有效但人已被停用时不该刷新时间戳。
    await _touch(token_id)
    return identity


class BearerGate:
    """挂在 MCP 端点外面的 ASGI 门槛。

    刻意只用 ASGI 而不是 FastAPI 依赖：MCP 端点是个裸 ASGI 应用，
    走的是 JSON-RPC 而不是 FastAPI 的请求模型，依赖注入在这里接不上。

    `prefix` 不是装饰：这个应用挂在根路径上（挂 `/mcp` 会被 Starlette
    用 307 重定向到 `/mcp/`，见 app/mcp_server/server.py 的说明），
    所以落到这里的任何路径都得先判断是不是 MCP 的，不是就直接 404——
    否则每一个 404 都会变成一次「MCP 通道收到了奇怪请求」的日志噪音。
    """

    def __init__(self, app, *, prefix: str = "/mcp") -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"

        if path != self.prefix and not path.startswith(self.prefix + "/"):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return

        raw = _bearer_value(Headers(scope=scope).get("authorization"))
        identity = await identity_for_token(raw) if raw else None

        if identity is None:
            logger.warning(
                "MCP 通道拒绝未授权请求。",
                extra={
                    "event": "mcp_unauthorized",
                    "path": path,
                    "client": scope.get("client", (None, None))[0],
                    # 只记「有没有带凭据」，不记凭据本身——日志会被复制走。
                    "credential_present": bool(raw),
                },
            )
            response = JSONResponse(
                {"error": "unauthorized", "detail": "需要有效的 Bearer 令牌。"},
                status_code=401,
                headers={"WWW-Authenticate": REALM},
            )
            await response(scope, receive, send)
            return

        # 交给下游：工具不必再查一次库，也不需要相信请求头里的任何身份字段。
        scope.setdefault("state", {})[STATE_KEY] = identity

        await self.app(scope, receive, send)


async def identity_from_ctx(ctx) -> Identity | None:
    """从工具上下文取出门槛已解析的身份。

    取不到（例如单测直接调用工具函数）时回落到请求头再解析一次——
    回落路径只读 Authorization，**永远不读**身份头。
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    state = getattr(request, "state", None) if request is not None else None
    cached = getattr(state, STATE_KEY, None) if state is not None else None

    if isinstance(cached, Identity):
        return cached

    headers = getattr(ctx, "headers", None)
    raw = _bearer_value(headers.get("authorization") if headers else None)

    return await identity_for_token(raw) if raw else None
