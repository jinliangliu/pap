# app/services/rate_limit_service.py
"""租户 + 用户 + 工具三维限流，以及租户日配额。

计数必须跨进程共享：N 个 worker 各自计数等于把配额放大 N 倍。
因此默认后端是 Redis（`RATE_LIMIT_BACKEND=auto` 且配置了 `REDIS_URL`），
进程内计数只保留给本地调试与测试。生产环境缺少 Redis 会直接拒绝启动。

为什么键里必须有 tenant：

- 一个租户刷满配额不能影响另一个租户（资源隔离，不只是数据隔离）；
- 同一个外部用户在两个租户里的额度互不相关；
- 统计与排查能按租户切分。

限流不能代替审批、幂等键与外部回读。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)

# 固定窗口计数。INCR 与 PEXPIRE 必须原子执行：
# 分开两次调用时，进程在中间崩溃会留下永不过期的键，等于该用户被永久限流。
_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return current
"""

_WINDOW_SECONDS = 60
_KEY_TTL_MS = (_WINDOW_SECONDS + 1) * 1000
_DAILY_TTL_MS = 25 * 60 * 60 * 1000


class RateLimitExceeded(Exception):
    """超出调用频率或配额。"""


class TenantQuotaExceeded(RateLimitExceeded):
    """租户级配额用尽（与单用户限流区分，提示语不同）。"""


class RateLimitBackendUnavailable(RateLimitExceeded):
    """限流后端不可用且策略为 fail-closed。

    继承 RateLimitExceeded：对调用方来说结果同样是「拒绝这次调用」，
    只是原因不同，提示语由异常消息携带。
    """


class RateLimiterProtocol(Protocol):
    backend: Literal["redis", "memory"]

    async def enforce(
        self,
        *,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        is_write: bool,
        tenant_limit_per_minute: int | None = None,
    ) -> None: ...

    async def enforce_daily_quota(
        self,
        *,
        tenant_id: str,
        resource: str,
        limit: int,
    ) -> None: ...

    async def ping(self) -> bool: ...


def _limit_for(is_write: bool) -> int:
    return (
        settings.write_tool_rate_limit_per_minute
        if is_write
        else settings.read_tool_rate_limit_per_minute
    )


def _bucket() -> int:
    return int(time.time()) // _WINDOW_SECONDS


def _day_bucket() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _tenant_tool_limit(override: int | None) -> int:
    if override and override > 0:
        return override

    return max(0, settings.default_tenant_tool_calls_per_minute)


class InMemoryRateLimiter:
    """单进程固定窗口计数。

    只用于本地调试与测试。多进程下每个进程各有一份计数，
    配额会被放大到「单进程上限 × 进程数」。
    """

    backend: Literal["memory"] = "memory"

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _incr(self, key: str) -> int:
        async with self._lock:
            # 简单清理旧键，避免无界增长。
            if len(self._counters) > 10_000:
                self._counters.clear()

            count = self._counters.get(key, 0) + 1
            self._counters[key] = count

        return count

    async def enforce(
        self,
        *,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        is_write: bool,
        tenant_limit_per_minute: int | None = None,
    ) -> None:
        bucket = _bucket()
        user_key = f"u:{tenant_id}:{user_id}:{tool_name}:{bucket}"
        count = await self._incr(user_key)

        if count > _limit_for(is_write):
            raise RateLimitExceeded("操作请求过于频繁，请稍后再试。")

        tenant_key = f"t:{tenant_id}:{bucket}"
        tenant_count = await self._incr(tenant_key)

        if tenant_count > _tenant_tool_limit(tenant_limit_per_minute):
            raise TenantQuotaExceeded("当前组织的工具调用额度已用尽，请稍后重试。")

    async def enforce_daily_quota(
        self,
        *,
        tenant_id: str,
        resource: str,
        limit: int,
    ) -> None:
        if limit <= 0:
            return

        key = f"d:{tenant_id}:{resource}:{_day_bucket()}"
        count = await self._incr(key)

        if count > limit:
            raise TenantQuotaExceeded("当前组织的当日额度已用尽，请明天再试或联系管理员。")

    async def ping(self) -> bool:
        return True


class RedisRateLimiter:
    """Redis 固定窗口计数，多进程/多实例共享同一份额度。"""

    backend: Literal["redis"] = "redis"

    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        timeout = settings.redis_timeout_seconds

        self._redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            health_check_interval=30,
        )

    async def enforce(
        self,
        *,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        is_write: bool,
        tenant_limit_per_minute: int | None = None,
    ) -> None:
        bucket = _bucket()
        limit = _limit_for(is_write)

        try:
            user_key = f"rate-limit:t:{tenant_id}:u:{user_id}:{tool_name}:{bucket}"
            count = await self._incr(user_key, _KEY_TTL_MS)

            if count > limit:
                raise RateLimitExceeded("操作请求过于频繁，请稍后再试。")

            tenant_key = f"rate-limit:t:{tenant_id}:tool:{bucket}"
            tenant_count = await self._incr(tenant_key, _KEY_TTL_MS)

            if tenant_count > _tenant_tool_limit(tenant_limit_per_minute):
                raise TenantQuotaExceeded("当前组织的工具调用额度已用尽，请稍后重试。")
        except RateLimitExceeded:
            raise
        except Exception as exc:
            # Redis 抖动时不能静默放行，否则限流等于不存在。
            # 需要可用性优先时把 RATE_LIMIT_FAIL_OPEN 打开。
            if settings.rate_limit_fail_open:
                logger.warning(
                    "限流后端不可用，按 fail-open 放行。",
                    extra={"event": "rate_limit_backend_error", "error": str(exc)},
                )
                return

            logger.error(
                "限流后端不可用，按 fail-closed 拒绝请求。",
                extra={"event": "rate_limit_backend_error", "error": str(exc)},
            )
            raise RateLimitBackendUnavailable("限流服务暂时不可用，已拒绝本次请求。") from exc

    async def enforce_daily_quota(
        self,
        *,
        tenant_id: str,
        resource: str,
        limit: int,
    ) -> None:
        if limit <= 0:
            return

        key = f"quota:t:{tenant_id}:{resource}:{_day_bucket()}"

        try:
            count = await self._incr(key, _DAILY_TTL_MS)
        except Exception as exc:
            if settings.rate_limit_fail_open:
                logger.warning(
                    "配额后端不可用，按 fail-open 放行。",
                    extra={"event": "quota_backend_error", "error": str(exc)},
                )
                return

            raise RateLimitBackendUnavailable("配额服务暂时不可用，已拒绝本次请求。") from exc

        if count > limit:
            raise TenantQuotaExceeded("当前组织的当日额度已用尽，请明天再试或联系管理员。")

    async def _incr(self, key: str, ttl_ms: int) -> int:
        result = await self._redis.eval(_INCR_WITH_TTL, 1, key, ttl_ms)
        return int(result)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        try:
            # redis-py >= 5 提供 aclose；pyproject 已要求 >= 5.2。
            await self._redis.aclose()
        except Exception:
            logger.debug("关闭 Redis 连接时出错。", exc_info=True)


class _RateLimiterProxy:
    """稳定引用。

    ToolGateway 在构造时拿到限流器；如果直接持有具体实例，
    启动阶段再切换后端（Redis 连通性检查之后）就会失效。
    代理保证调用时永远解析到当前后端。
    """

    def __init__(self) -> None:
        self._delegate: RateLimiterProtocol | None = None

    @property
    def backend(self) -> Literal["redis", "memory"]:
        return self._resolve().backend

    async def enforce(
        self,
        *,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        is_write: bool,
        tenant_limit_per_minute: int | None = None,
    ) -> None:
        await self._resolve().enforce(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            is_write=is_write,
            tenant_limit_per_minute=tenant_limit_per_minute,
        )

    async def enforce_daily_quota(
        self,
        *,
        tenant_id: str,
        resource: str,
        limit: int,
    ) -> None:
        await self._resolve().enforce_daily_quota(
            tenant_id=tenant_id,
            resource=resource,
            limit=limit,
        )

    async def ping(self) -> bool:
        return await self._resolve().ping()

    def _resolve(self) -> RateLimiterProtocol:
        if self._delegate is None:
            # 未经启动钩子（测试、脚本）时按当前配置即时构造。
            self._delegate = _build_limiter()
        return self._delegate


_proxy = _RateLimiterProxy()


def _build_limiter() -> RateLimiterProtocol:
    backend = settings.resolved_rate_limit_backend
    url = (settings.redis_url or "").strip()

    if backend == "redis":
        if not url:
            raise RuntimeError("限流后端要求 Redis，但 REDIS_URL 为空。")
        return RedisRateLimiter(url)

    if settings.is_production:
        # 配置校验已拦截，这里再兜一层，避免绕过 Settings 的实例进入生产。
        raise RuntimeError("生产环境不允许进程内限流：请配置 REDIS_URL。")

    return InMemoryRateLimiter()


async def init_rate_limiter() -> RateLimiterProtocol:
    """启动期确定后端并做一次真实连通性检查。"""
    limiter = _build_limiter()

    if limiter.backend == "redis" and not await limiter.ping():
        message = f"Redis 不可达，无法启用跨进程限流。REDIS_URL={settings.redis_url!r}"

        if settings.app_env == "test":
            logger.warning(message)
            limiter = InMemoryRateLimiter()
        elif settings.is_production or settings.rate_limit_backend == "redis":
            raise RuntimeError(message)
        else:
            # 开发环境允许降级，但必须留下明确日志，避免误以为限流生效。
            logger.warning(
                "%s 已降级为进程内计数，多进程下配额不共享。",
                message,
                extra={"event": "rate_limit_degraded"},
            )
            limiter = InMemoryRateLimiter()

    _proxy._delegate = limiter

    logger.info(
        "限流后端已就绪。",
        extra={"event": "rate_limiter_ready", "backend": limiter.backend},
    )

    return limiter


async def close_rate_limiter() -> None:
    """关闭后端连接，但保留代理对象本身（Gateway 持有它的引用）。"""
    delegate: Any = _proxy._delegate
    _proxy._delegate = None

    close = getattr(delegate, "close", None)

    if close is not None:
        await close()


def get_rate_limiter() -> RateLimiterProtocol:
    return _proxy
