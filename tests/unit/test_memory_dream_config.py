# tests/unit/test_memory_dream_config.py
"""租户级梦境配置：三层合并、0 值语义、写入不展开默认值。

三条最该守住的东西：

1. 平台关闭是急停：租户配了 `enabled=True` 也必须关着，否则运维的开关形同虚设；
2. `0` / `NULL` 表示「跟随平台默认」，落库时就存 0，**不能展开成当前默认值**——
   展开等于把今天的默认值钉死在这个租户上，以后平台调默认它就不跟随了；
3. 单次条数超上限按上限跑，不抛错（前端滑块与配置不同步是常态，
   为了「你填多了」让整次梦境失败没有价值）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.memory import TenantMemorySettings
from app.services import memory_dream
from app.tenancy.context import tenant_scope

TENANT = "default"


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    """`seeded_identity` 负责建出租户行（配置表有指向 tenants 的外键）。"""
    await session.execute(delete(TenantMemorySettings))
    await session.commit()
    yield
    await session.execute(delete(TenantMemorySettings))
    await session.commit()


async def _resolve():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            return await memory_dream.resolve_dream_config(session, tenant_id=TENANT)


async def _save(*, enabled=None, days=0, max_per_run=0):
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            config = await memory_dream.save_dream_config(
                session,
                tenant_id=TENANT,
                enabled=enabled,
                days=days,
                max_per_run=max_per_run,
                actor_user_id="actor-1",
            )
            await session.commit()
            return config


async def _rows():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            return list((await session.scalars(select(TenantMemorySettings))).all())


async def test_no_row_falls_back_to_platform_defaults():
    """存量为空是常态，不能因此读不出配置。"""
    config = await _resolve()

    assert config.enabled is True
    assert config.days == settings.memory_dream_days
    assert config.max_per_run == settings.memory_dream_max_per_run
    assert config.tenant_enabled is None
    assert config.tenant_days == 0
    assert config.disabled_reason is None


async def test_tenant_overrides_are_effective():
    await _save(enabled=True, days=7, max_per_run=3)
    config = await _resolve()

    assert config.days == 7
    assert config.max_per_run == 3
    assert config.tenant_days == 7
    assert config.tenant_max_per_run == 3


async def test_zero_means_follow_platform_not_zero_days():
    """0 是「不覆盖」，不是「0 天」。写成 0 天会让梦境扫不到任何证据。"""
    await _save(enabled=None, days=0, max_per_run=0)
    config = await _resolve()

    assert config.days == settings.memory_dream_days
    assert config.max_per_run == settings.memory_dream_max_per_run
    assert config.tenant_days == 0
    assert config.tenant_max_per_run == 0


async def test_platform_off_cannot_be_overridden_by_tenant(monkeypatch):
    """急停优先。这条反了，运维想临时止血就得挨个改租户。"""
    monkeypatch.setattr(settings, "memory_dream_enabled", False)

    await _save(enabled=True)
    config = await _resolve()

    assert config.enabled is False
    assert config.platform_enabled is False
    assert config.tenant_enabled is True
    assert "平台层已关闭" in (config.disabled_reason or "")


async def test_tenant_can_turn_off_while_platform_on():
    await _save(enabled=False)
    config = await _resolve()

    assert config.enabled is False
    assert config.platform_enabled is True
    assert config.disabled_reason == "本租户已关闭梦境"


async def test_inherit_keeps_enabled_when_platform_on():
    await _save(enabled=None)
    config = await _resolve()

    assert config.enabled is True
    assert config.tenant_enabled is None
    assert config.disabled_reason is None


def test_clamp_limit_caps_and_floors():
    config = memory_dream.DreamConfig(
        enabled=True,
        days=30,
        max_per_run=5,
        platform_enabled=True,
        tenant_enabled=None,
        tenant_days=0,
        tenant_max_per_run=5,
    )

    assert config.clamp_limit(99) == 5
    assert config.clamp_limit(0) == memory_dream.MAX_PER_RUN_MIN
    assert config.clamp_limit(3) == 3


async def test_save_is_upsert_not_insert():
    """同一个租户反复保存只能有一行，否则 `session.get` 取到哪条不确定。"""
    await _save(days=10)
    await _save(days=20)

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].dream_days == 20


async def test_save_does_not_expand_platform_default():
    """落库必须保持 0，而不是写成 30。"""
    await _save(days=0, max_per_run=0)

    rows = await _rows()
    assert rows[0].dream_days == 0
    assert rows[0].dream_max_per_run == 0
    assert rows[0].updated_by == "actor-1"


@pytest.mark.parametrize(
    ("days", "max_per_run"),
    [(366, 0), (-1, 0), (0, 201), (0, -5)],
)
async def test_save_rejects_out_of_range(days, max_per_run):
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            with pytest.raises(ValueError):
                await memory_dream.save_dream_config(
                    session,
                    tenant_id=TENANT,
                    enabled=None,
                    days=days,
                    max_per_run=max_per_run,
                    actor_user_id="actor-1",
                )
