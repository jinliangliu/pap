# tests/unit/test_runtime_settings.py
"""运行配置：库里优先、缺失回落 .env、脏值按未配置处理。

这套用例守的是一条容易被改坏的边界：**默认值来自 `.env`，但一旦管理员在
控制台设过，就以库为准**。两个来源一旦混起来，会出现「控制台显示已开启、
重启后又回到关闭」这种最难查的故障。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.platform_setting import PlatformSetting
from app.services import runtime_settings


async def _write(value) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            session.add(PlatformSetting(key=runtime_settings.OBSERVABILITY_ENABLED, value=value))


@pytest.fixture(autouse=True)
async def _clean_settings():
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PlatformSetting))
    yield
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PlatformSetting))


async def test_defaults_to_enabled_without_any_record(monkeypatch):
    """默认开启：PAP 处在开发调试期，观测是排障依据，不该被「漏配」关掉。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)

    await runtime_settings.load()

    assert runtime_settings.obs_enabled() is True
    assert runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED) == "env"


async def test_env_fallback_still_works(monkeypatch):
    """要让环境变量压住默认值时仍然有效——这是「还没人配过」时的逃生口。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", False)

    await runtime_settings.load()

    assert runtime_settings.obs_enabled() is False
    assert runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED) == "env"


async def test_database_value_wins_over_env(monkeypatch):
    """库里有记录就以库为准：否则管理员改完、重启一次又回到 .env 的旧值。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", False)
    await _write(False)

    await runtime_settings.load()

    assert runtime_settings.obs_enabled() is False
    assert runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED) == "runtime"


async def test_dirty_value_falls_back_instead_of_guessing():
    """库里的 JSON 可能被手改。`"false"` 猜成 False 还是 True 都有风险，按未配置处理。"""
    for bad in ("false", 0, 1, None, {"v": True}, []):
        runtime_settings.reset_for_tests()
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(PlatformSetting))
                session.add(
                    PlatformSetting(key=runtime_settings.OBSERVABILITY_ENABLED, value=bad)
                )

        await runtime_settings.load()

        assert runtime_settings.obs_enabled() is True, bad
        assert runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED) == "env", bad


async def test_set_takes_effect_without_restart(monkeypatch):
    """保存后立刻生效：内存值当场变，库里有行，来源变 runtime。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)

    await runtime_settings.load()
    assert runtime_settings.obs_enabled() is True

    changed = await runtime_settings.set_observability(False, actor="pap-admin")

    assert changed is True
    assert runtime_settings.obs_enabled() is False
    assert runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED) == "runtime"

    async with async_session_factory() as session:
        row = await session.scalar(
            select(PlatformSetting).where(
                PlatformSetting.key == runtime_settings.OBSERVABILITY_ENABLED
            )
        )
    assert row is not None
    assert row.value is False
    assert row.updated_by == "pap-admin"


async def test_set_same_value_reports_no_change(monkeypatch):
    """重复保存同一个值不该被记成「发生变更」——审计里那会变成噪音。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)

    await runtime_settings.load()

    assert await runtime_settings.set_observability(True, actor="pap-admin") is False


async def test_load_survives_missing_table(monkeypatch):
    """表还没迁移时不能起不来：读取失败按默认值继续。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)

    class Boom:
        def __call__(self):
            raise RuntimeError("relation platform_settings does not exist")

    monkeypatch.setattr(runtime_settings, "async_session_factory", Boom())

    await runtime_settings.load()

    assert runtime_settings.obs_enabled() is True
