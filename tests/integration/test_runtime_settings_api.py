# tests/integration/test_runtime_settings_api.py
"""运行配置接口：只有平台管理员能改，改完立刻生效，且留审计。

这里特意验「改完内存里的开关真的变了」而不只是「接口返回了新值」——
一个只写库、不更新进程内缓存的实现，接口和页面都会显示成功，
重启前却什么都不生效。这类 bug 靠返回值断言抓不到。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.audit import AuditEventType, AuditLog
from app.models.platform_setting import PlatformSetting
from app.observability.writer import get_writer
from app.services import runtime_settings

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/runtime-settings"


@pytest.fixture(autouse=True)
async def _clean():
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PlatformSetting))
    yield
    await get_writer().set_enabled(False)
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PlatformSetting))


async def test_requires_platform_admin(client):
    assert (await client.get(BASE)).status_code in (401, 403)
    assert (
        await client.put(BASE, json={"observability_enabled": False})
    ).status_code in (401, 403)


async def test_defaults_to_enabled_and_says_it_comes_from_env(admin_client, monkeypatch):
    """库里没有记录时：默认开启，并明说这是 .env 的默认值而不是管理员设的。"""
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)
    runtime_settings.reset_for_tests()
    await runtime_settings.load()

    body = (await admin_client.get(BASE)).json()

    assert body["observability_enabled"] is True
    assert body["observability_source"] == "env"


async def test_toggle_takes_effect_immediately(admin_client):
    """关掉之后同一个进程里的开关与写入器都要立刻跟着变。"""
    await runtime_settings.load()
    assert runtime_settings.obs_enabled() is True

    response = await admin_client.put(BASE, json={"observability_enabled": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["observability_enabled"] is False
    assert body["observability_source"] == "runtime"

    # 内存值（span() 的热路径读的就是它）
    assert runtime_settings.obs_enabled() is False
    # 写入器任务确实停了
    assert get_writer().stats()["running"] is False

    # 落库了，重启后仍然是关闭
    async with async_session_factory() as session:
        row = await session.scalar(
            select(PlatformSetting).where(
                PlatformSetting.key == runtime_settings.OBSERVABILITY_ENABLED
            )
        )
    assert row is not None and row.value is False
    assert row.updated_by


async def test_toggle_writes_admin_audit(admin_client):
    await runtime_settings.load()

    await admin_client.put(BASE, json={"observability_enabled": False})

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == AuditEventType.ADMIN_ACTION)
                .where(AuditLog.message == "runtime_settings_updated")
            )
        ).all()

    assert rows, "运行配置变更必须留 ADMIN_ACTION"
    assert rows[-1].resource_id == runtime_settings.OBSERVABILITY_ENABLED
