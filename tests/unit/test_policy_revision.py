# tests/unit/test_policy_revision.py
"""策略版本：变了才 +1，每一版都能按号取回。

这套用例守的是「事后能回答当时按哪版跑」这件事。它最容易以两种方式失效：

- 没变也 +1：历史里塞满空转版本，按号排查时要逐条排除；
- 变了不落快照：号在涨，但取回那一版时是空的——比没有版本号更坏，
  因为它看起来有据可查。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.platform_setting import PlatformSetting
from app.models.policy_snapshot import PolicySnapshot
from app.services import runtime_settings


@pytest.fixture(autouse=True)
async def _clean():
    await _reset()
    yield
    await _reset()


async def _reset() -> None:
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(PolicySnapshot))
            await session.execute(delete(PlatformSetting))


async def test_revision_zero_is_synthesized_from_env_defaults(monkeypatch):
    """空库环境的 revision 是 0，且这一版有内容——由代码合成，不占一行。

    若 0 返回 None，第一条工具调用记下的版本号就成了一个查不到的引用。
    """
    monkeypatch.setattr(runtime_settings.settings, "obs_enabled", True)
    await runtime_settings.load()

    assert runtime_settings.current_revision() == 0

    payload = await runtime_settings.snapshot_at(0)

    assert payload is not None
    assert payload[runtime_settings.DEFAULT_TOOL_ACTION] == "allow"
    assert payload[runtime_settings.TOOL_BACKUP_THRESHOLD_MB] == 100
    assert payload[runtime_settings.TOOL_BACKUP_MAX_SIZE_MB] == 3000


async def test_change_bumps_revision_and_keeps_the_previous_one():
    """改一次 +1，旧的那一版仍然按旧号取得到——这是「快照」而不是「当前值」。"""
    await runtime_settings.load()

    changed = await runtime_settings.set_default_tool_action("deny", actor="tester")

    assert changed is True
    assert runtime_settings.current_revision() == 1

    newest = await runtime_settings.snapshot_at(1)
    oldest = await runtime_settings.snapshot_at(0)

    assert newest is not None and oldest is not None
    assert newest[runtime_settings.DEFAULT_TOOL_ACTION] == "deny"
    # 第 0 版必须是**改之前**的样子，不能跟着内存一起变。
    assert oldest[runtime_settings.DEFAULT_TOOL_ACTION] == "allow"


async def test_snapshot_carries_the_whole_policy_not_just_the_changed_key():
    """一次只改一项，快照里仍是完整的一份。

    只存变更的话，取回第 N 版要重放前面所有版本，而重放依赖每一版都在。
    """
    await runtime_settings.load()

    await runtime_settings.set_default_tool_action("deny", actor="tester")
    await runtime_settings.set_backup_threshold_mb(20, actor="tester")

    payload = await runtime_settings.snapshot_at(2)

    assert payload is not None
    assert payload[runtime_settings.DEFAULT_TOOL_ACTION] == "deny"
    assert payload[runtime_settings.TOOL_BACKUP_THRESHOLD_MB] == 20
    assert payload[runtime_settings.KNOWLEDGE_MAX_CHUNKS] >= 1


async def test_setting_the_same_value_does_not_bump():
    """保存成同一个值时行为没变，就不该多出一个版本。"""
    await runtime_settings.load()

    first = await runtime_settings.set_default_tool_action("allow", actor="tester")
    second = await runtime_settings.set_default_tool_action("allow", actor="tester")

    assert first is False
    assert second is False
    assert runtime_settings.current_revision() == 0


async def test_load_restores_revision_from_database():
    """重启之后版本号要接上：否则新进程从 0 起，和库里的历史对不上。"""
    await runtime_settings.load()
    await runtime_settings.set_default_tool_action("deny", actor="tester")

    runtime_settings.reset_for_tests()
    await runtime_settings.load()

    assert runtime_settings.current_revision() == 1


async def test_unknown_revision_returns_none():
    """库里没有的号返回 None，由调用方决定怎么说；不抛、也不编一份默认值。"""
    await runtime_settings.load()

    assert await runtime_settings.snapshot_at(99) is None
    assert await runtime_settings.snapshot_at(-1) is None


async def test_dirty_backup_value_falls_back_instead_of_guessing():
    """脏值按「未配置」处理：阈值被手改成字符串时应当回落 100，而不是生效为 0。

    生效成 0 会让每一次备份都被判超阈值——现象是「备份开关开着，却一个都没有」。
    """
    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                PlatformSetting(
                    key=runtime_settings.TOOL_BACKUP_THRESHOLD_MB,
                    value="100",
                )
            )

    await runtime_settings.load()

    assert runtime_settings.backup_threshold_mb() == 100
    assert (
        runtime_settings.source(runtime_settings.TOOL_BACKUP_THRESHOLD_MB) == "env"
    )
