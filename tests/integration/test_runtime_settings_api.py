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
from app.models.policy_snapshot import PolicySnapshot
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
            await session.execute(delete(PolicySnapshot))
    yield
    await get_writer().set_enabled(False)
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PlatformSetting))
            await session.execute(delete(PolicySnapshot))


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


# ---------------- 入库容量（单篇切片上限） ----------------


async def test_chunk_limit_change_does_not_touch_observability(admin_client, monkeypatch):
    """只改容量时，观测开关必须原样不动。

    原先两个字段都得传，于是「改容量」的请求会顺带把观测开关按页面手里的
    旧值再写一遍——两个人同时开着这个页面时，后保存的人会静默改回别人的设置。
    """
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)
    await runtime_settings.load()

    await admin_client.put(BASE, json={"observability_enabled": False})
    assert runtime_settings.obs_enabled() is False

    response = await admin_client.put(BASE, json={"knowledge_max_chunks": 3000})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["knowledge_max_chunks"] == 3000
    assert body["knowledge_max_chunks_source"] == "runtime"
    assert body["observability_enabled"] is False
    assert body["observability_source"] == "runtime"


async def test_chunk_limit_out_of_range_is_rejected(admin_client):
    for bad in (0, 20001):
        response = await admin_client.put(BASE, json={"knowledge_max_chunks": bad})
        assert response.status_code == 422, response.text


async def test_chunk_limit_change_is_audited_under_its_own_key(admin_client):
    """审计要能单独查到「谁放宽了容量」，而不是混在观测开关那条里。"""
    await runtime_settings.load()

    await admin_client.put(BASE, json={"knowledge_max_chunks": 3000})

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == AuditEventType.ADMIN_ACTION)
                .where(AuditLog.message == "runtime_settings_updated")
            )
        ).all()

    assert rows, "运行配置变更必须留 ADMIN_ACTION"
    # 审计表不随 platform_settings 一起清，所以只断言「最近一条」是容量那条：
    # 同一用例文件里前面还有观测开关的审计，按集合相等会比较到别人的行。
    assert rows[-1].resource_id == runtime_settings.KNOWLEDGE_MAX_CHUNKS


# ---------------- 策略版本与快照 ----------------


async def test_changing_a_setting_returns_a_new_revision(admin_client):
    """改一次 +1，并且这次响应里就能看到新号。

    页面靠这个号回答「我刚才那次保存生效了没有」——不给号，人只能再发一次
    请求去比对，而那次请求与保存之间别人可能又改了一次。
    """
    await runtime_settings.load()

    before = (await admin_client.get(BASE)).json()["revision"]

    body = (await admin_client.put(BASE, json={"knowledge_max_chunks": 1234})).json()

    assert body["revision"] > before
    assert body["knowledge_max_chunks"] == 1234


async def test_snapshot_endpoint_returns_the_version_that_was_used(admin_client):
    """按号取回当时那一版：审计里记的是号，这里是号对应的值。"""
    await runtime_settings.load()

    first = (await admin_client.put(BASE, json={"knowledge_max_chunks": 1111})).json()
    revision = first["revision"]

    await admin_client.put(BASE, json={"knowledge_max_chunks": 2222})

    snapshot = (
        await admin_client.get(f"{BASE}/snapshots/{revision}")
    ).json()

    assert snapshot["found"] is True
    assert snapshot["revision"] == revision
    # 取回的是**当时**那份，不是现在的 2222。
    assert snapshot["settings"][runtime_settings.KNOWLEDGE_MAX_CHUNKS] == 1111


async def test_snapshot_of_an_unknown_revision_says_not_found(admin_client):
    """查不到返回 found=false 而不是 404：「这一版不存在」与「接口不存在」
    对调用方是两种完全不同的处置。"""
    runtime_settings.reset_for_tests()
    await runtime_settings.load()

    body = (await admin_client.get(f"{BASE}/snapshots/999999")).json()

    assert body["found"] is False
    assert body["settings"] == {}


async def test_revision_zero_is_always_available(admin_client):
    """0 表示「从未改过配置」，由代码合成——它是第一条工具调用所处的策略，
    所以不能查不到。"""
    runtime_settings.reset_for_tests()
    await runtime_settings.load()

    body = (await admin_client.get(f"{BASE}/snapshots/0")).json()

    assert body["found"] is True
    assert body["settings"][runtime_settings.DEFAULT_TOOL_ACTION] == "allow"


async def test_backup_settings_are_persisted_and_audited_separately(admin_client):
    """备份三项各自一条审计：混成一条就查不出「谁把阈值调小了」。"""
    await runtime_settings.load()

    response = await admin_client.put(
        BASE,
        json={
            "tool_backup_enabled": True,
            "tool_backup_threshold_mb": 50,
            "tool_backup_max_size_mb": 2000,
        },
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["tool_backup_threshold_mb"] == 50
    assert body["tool_backup_max_size_mb"] == 2000
    assert body["tool_backup_threshold_mb_source"] == "runtime"

    assert runtime_settings.backup_threshold_mb() == 50

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == AuditEventType.ADMIN_ACTION)
                .where(AuditLog.message == "runtime_settings_updated")
            )
        ).all()

    resources = {row.resource_id for row in rows}
    assert runtime_settings.TOOL_BACKUP_THRESHOLD_MB in resources
    assert runtime_settings.TOOL_BACKUP_MAX_SIZE_MB in resources


async def test_backup_size_out_of_range_is_rejected(admin_client):
    """尺寸项不做夹取：夹了之后人看到的数字和填的对不上，会以为没保存。"""
    for bad in (0, 200000):
        response = await admin_client.put(BASE, json={"tool_backup_threshold_mb": bad})
        assert response.status_code == 422, response.text
