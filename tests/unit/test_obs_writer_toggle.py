# tests/unit/test_obs_writer_toggle.py
"""观测写入器能被运行时开关起停，不需要重启进程。

守两件事：

1. `start()` 看的是**运行配置**，不是 `settings.obs_enabled`。改之前它直接读
   `.env`，于是「管理员在控制台开了」与「进程还按旧值跑」会长期不一致。
2. 关→开→关连续切换都能正确起停。`stop()` 把 task 与队列都置空，
   重新 `start()` 必须能重建——这是动态开关成立的前提。
"""

from __future__ import annotations

from app.core.config import settings
from app.observability import writer as writer_module
from app.observability.writer import ObsWriter
from app.services import runtime_settings


async def test_start_uses_runtime_setting_not_env(monkeypatch):
    """.env 是关的，运行配置是开的——应当起任务。"""
    monkeypatch.setattr(writer_module.runtime_settings, "obs_enabled", lambda: True)
    monkeypatch.setattr(settings, "obs_enabled", False)

    obs = ObsWriter()
    await obs.set_enabled(True)
    try:
        assert obs.stats()["running"] is True
    finally:
        await obs.stop()


async def test_stops_and_restarts_cleanly(monkeypatch):
    """关掉再打开：队列与任务都要重建，不能留下上一次的残留。"""
    monkeypatch.setattr(writer_module.runtime_settings, "obs_enabled", lambda: True)

    obs = ObsWriter()
    await obs.set_enabled(True)
    assert obs.stats()["running"] is True

    await obs.set_enabled(False)
    assert obs.stats()["running"] is False
    assert obs.stats()["queue_depth"] == 0

    await obs.set_enabled(True)
    assert obs.stats()["running"] is True

    await obs.stop()


async def test_disabled_writer_drops_submissions(monkeypatch):
    """关闭后 submit 直接丢弃：不做采样、不进队列、不计入丢弃数。"""
    monkeypatch.setattr(writer_module.runtime_settings, "obs_enabled", lambda: False)

    obs = ObsWriter()
    await obs.set_enabled(True)

    obs.submit({"trace": {"trace_id": "t-1"}, "type": "start", "spans": []})

    assert obs.stats()["running"] is False
    assert obs.stats()["queue_depth"] == 0
    assert obs.stats()["dropped"] == 0


async def test_runtime_setting_is_read_per_call(monkeypatch):
    """热路径每次读当前值：切换后同一进程里的 span() 立刻跟着变。"""
    runtime_settings.reset_for_tests()
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)

    assert runtime_settings.obs_enabled() is True

    monkeypatch.setattr(runtime_settings, "_cache", {})
    monkeypatch.setattr(runtime_settings, "_from_db", set())
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", False)

    assert runtime_settings.obs_enabled() is False
    runtime_settings.reset_for_tests()


async def test_consumer_does_not_inherit_caller_tenant(monkeypatch):
    """从管理接口里开启时，消费者不能继承那个请求的租户。

    踩过的坑：`PUT /admin/runtime-settings` 跑在 platform_admin 作用域下，
    `create_task` 默认复制当前 context，消费者于是带着 `platform` 出生；
    之后每批写库 `tenant_scope("default")` 都会抛「拒绝在同一执行流内切换租户」，
    表现是**接口 200、界面显示已开启、库里一条没有**，只在日志里留几行
    `obs_write_failed`。
    """
    import asyncio

    from app.tenancy.context import (
        bind_tenant,
        current_tenant,
        platform_admin_tenant_scope,
        unbind_tenant,
    )

    monkeypatch.setattr(writer_module.runtime_settings, "obs_enabled", lambda: True)
    monkeypatch.setattr(writer_module, "_FLUSH_INTERVAL", 0.05)

    seen: list[tuple[str, str | None]] = []

    async def fake_flush(self, batch):
        seen.append((batch[0]["tenant_id"], current_tenant()))

    monkeypatch.setattr(ObsWriter, "_flush", fake_flush)

    obs = ObsWriter()
    # 与 require_platform_admin 一致：先把上下文固定到平台租户，再执行管理动作。
    token = bind_tenant("platform")
    try:
        with platform_admin_tenant_scope("platform"):
            await obs.set_enabled(True)
    finally:
        unbind_tenant(token)

    obs.submit({"tenant_id": "default", "trace": {"trace_id": "t-1"}, "type": "start", "spans": []})
    await asyncio.sleep(0.25)
    await obs.stop()

    assert seen, "消费者没有处理这批数据"
    assert seen[0][0] == "default"
    # 消费者本身必须不属于任何租户，否则 tenant_scope 会拒绝切换
    assert seen[0][1] != "platform"
