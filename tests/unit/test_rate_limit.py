# tests/unit/test_rate_limit.py
"""限流必须跨进程共享，且后端故障时行为明确。"""

from __future__ import annotations

import pytest

from app.services import rate_limit_service as rl


class FakeRedis:
    """只实现 INCR 语义与 ping 的最小替身，两个限流器可共享同一份计数。"""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, key: str, ttl_ms: int):  # noqa: ANN201
        assert numkeys == 1
        count = self.counters.get(key, 0) + 1
        self.counters[key] = count

        if count == 1:
            self.ttls[key] = int(ttl_ms)

        return count

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class BrokenRedis:
    async def eval(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("connection refused")

    async def ping(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None


def _limiter(redis=None) -> rl.RedisRateLimiter:  # noqa: ANN001
    limiter = rl.RedisRateLimiter("redis://127.0.0.1:6379/0")
    limiter._redis = redis if redis is not None else FakeRedis()  # noqa: SLF001
    return limiter


async def test_counter_is_shared_between_processes(monkeypatch):
    """两个 RedisRateLimiter 实例代表两个 worker，它们必须共用同一份额度。"""
    monkeypatch.setattr(rl.settings, "write_tool_rate_limit_per_minute", 2)
    monkeypatch.setattr(rl.settings, "redis_timeout_seconds", 1.0)
    monkeypatch.setattr(rl.settings, "default_tenant_tool_calls_per_minute", 1000)

    shared = FakeRedis()
    worker_a = _limiter(shared)
    worker_b = _limiter(shared)

    await worker_a.enforce(tenant_id="t1", user_id="u1", tool_name="write_tool", is_write=True)
    await worker_b.enforce(tenant_id="t1", user_id="u1", tool_name="write_tool", is_write=True)

    with pytest.raises(rl.RateLimitExceeded):
        await worker_a.enforce(tenant_id="t1", user_id="u1", tool_name="write_tool", is_write=True)


async def test_counter_key_has_ttl(monkeypatch):
    """计数键必须带过期时间，否则会永久占住配额。"""
    monkeypatch.setattr(rl.settings, "redis_timeout_seconds", 1.0)

    shared = FakeRedis()
    await _limiter(shared).enforce(
        tenant_id="t1", user_id="u1", tool_name="read_tool", is_write=False
    )

    assert shared.ttls
    assert all(ttl >= 60_000 for ttl in shared.ttls.values())


async def test_quota_is_isolated_between_tenants(monkeypatch):
    """一个租户用满额度，不能影响另一个租户。"""
    monkeypatch.setattr(rl.settings, "write_tool_rate_limit_per_minute", 100)
    monkeypatch.setattr(rl.settings, "default_tenant_tool_calls_per_minute", 2)

    limiter = _limiter(FakeRedis())

    for _ in range(2):
        await limiter.enforce(tenant_id="t1", user_id="u1", tool_name="write_tool", is_write=True)

    with pytest.raises(rl.TenantQuotaExceeded):
        await limiter.enforce(tenant_id="t1", user_id="u1", tool_name="write_tool", is_write=True)

    # 另一个租户仍然有完整额度。
    await limiter.enforce(tenant_id="t2", user_id="u1", tool_name="write_tool", is_write=True)


async def test_counter_keys_are_namespaced_by_tenant(monkeypatch):
    """同一个用户在两个租户里必须落在不同的计数键上。"""
    monkeypatch.setattr(rl.settings, "redis_timeout_seconds", 1.0)

    shared = FakeRedis()
    limiter = _limiter(shared)

    await limiter.enforce(tenant_id="t1", user_id="u1", tool_name="read_tool", is_write=False)
    await limiter.enforce(tenant_id="t2", user_id="u1", tool_name="read_tool", is_write=False)

    user_keys = [key for key in shared.counters if ":u:u1:" in key]

    assert len(user_keys) == 2
    assert all(key.startswith("rate-limit:t:") for key in user_keys)


async def test_backend_failure_fails_closed_by_default(monkeypatch):
    monkeypatch.setattr(rl.settings, "rate_limit_fail_open", False)

    with pytest.raises(rl.RateLimitBackendUnavailable):
        await _limiter(BrokenRedis()).enforce(
            tenant_id="t1",
            user_id="u1",
            tool_name="write_tool",
            is_write=True,
        )


async def test_backend_failure_can_fail_open(monkeypatch):
    """可用性优先时允许放行，但必须打日志而不是静默。"""
    monkeypatch.setattr(rl.settings, "rate_limit_fail_open", True)

    await _limiter(BrokenRedis()).enforce(
        tenant_id="t1",
        user_id="u1",
        tool_name="write_tool",
        is_write=True,
    )


async def test_init_falls_back_to_memory_in_test_env(monkeypatch):
    """测试环境不依赖外部 Redis；降级后必须能明确看出后端是 memory。"""
    monkeypatch.setattr(rl.settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(rl.settings, "rate_limit_backend", "auto")

    limiter = await rl.init_rate_limiter()

    assert limiter.backend == "memory"
    assert rl.get_rate_limiter().backend == "memory"

    await rl.close_rate_limiter()


async def test_memory_backend_is_used_without_redis_url():
    limiter = await rl.init_rate_limiter()

    assert limiter.backend == "memory"

    await rl.close_rate_limiter()
