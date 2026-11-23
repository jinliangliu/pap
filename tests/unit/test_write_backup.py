# tests/unit/test_write_backup.py
"""写前备份：三个阈值的实际语义。

这套用例盯的是「什么情况下没备份」——那是最容易被悄悄漏掉的一半。
`captured=False` 必须带着原因（`skipped`），否则事后读日志的人只能猜：
是没声明钩子、开关关了、还是超了阈值、还是存满了？四种情况的处置完全不同。
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.db.session import async_session_factory
from app.models.platform_setting import PlatformSetting
from app.models.policy_snapshot import PolicySnapshot
from app.models.write_backup import WriteBackup
from app.services import runtime_settings, write_backup_service
from app.tools.types import BackupSpec
from tests.conftest import TEST_TENANT

_MB = 1024 * 1024


@pytest.fixture(autouse=True)
async def _clean(seeded_identity):
    await _reset()
    yield
    await _reset()


async def _reset() -> None:
    runtime_settings.reset_for_tests()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(WriteBackup))
            await session.execute(delete(PolicySnapshot))
            await session.execute(delete(PlatformSetting))
    await runtime_settings.load()


async def _row_count() -> int:
    async with async_session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(WriteBackup)) or 0)


def _spec(blob: str = "x") -> BackupSpec:
    return BackupSpec(
        resource_type="workflow",
        resource_id="wf-1",
        payload={"definition": blob},
    )


async def test_capture_stores_the_original_and_reports_size():
    outcome = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="studio_workflow_save",
        decision="approved",
        policy_revision=3,
        spec=_spec(),
        run_id="run-1",
    )

    assert outcome.enabled is True
    assert outcome.captured is True
    assert outcome.skipped is None
    assert outcome.size_bytes > 0
    assert outcome.backup_id

    async with async_session_factory() as session:
        row = await session.get(WriteBackup, uuid.UUID(str(outcome.backup_id)))

    assert row is not None
    assert row.payload["definition"] == "x"
    assert row.policy_revision == 3
    assert row.decision == "approved"


async def test_over_threshold_is_skipped_with_a_reason():
    """超过阈值就不备，且必须说清是被阈值挡掉的。

    静默跳过会让「原件还在」在事后变成一句假话。
    """
    await runtime_settings.set_backup_threshold_mb(1, actor="tester")

    outcome = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="studio_workflow_save",
        decision="approved",
        policy_revision=1,
        spec=_spec("x" * (_MB + 1024)),
    )

    assert outcome.enabled is True
    assert outcome.captured is False
    assert outcome.skipped == "over_threshold"
    assert outcome.threshold_mb == 1
    assert await _row_count() == 0


async def test_store_full_stops_instead_of_overwriting():
    """总量到顶就停，**不覆盖最旧的一份**。

    覆盖能让上限看起来「永远够用」，代价是「早期那次操作的原件」在某天
    无声消失——而人正是靠它来回滚的。
    """
    await runtime_settings.set_backup_threshold_mb(100, actor="tester")
    await runtime_settings.set_backup_max_size_mb(1, actor="tester")

    first = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="t",
        decision="approved",
        policy_revision=1,
        spec=_spec("a" * (700 * 1024)),
    )
    second = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="t",
        decision="approved",
        policy_revision=1,
        spec=_spec("b" * (700 * 1024)),
    )

    assert first.captured is True
    assert second.captured is False
    assert second.skipped == "store_full"

    async with async_session_factory() as session:
        rows = (await session.scalars(select(WriteBackup))).all()

    # 第一条还在，且内容没被后来者顶掉。
    assert len(rows) == 1
    assert rows[0].payload["definition"].startswith("a")


async def test_master_switch_off_skips_everything():
    await runtime_settings.set_backup_enabled(False, actor="tester")

    outcome = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="t",
        decision="approved",
        policy_revision=1,
        spec=_spec(),
    )

    assert outcome.enabled is False
    assert outcome.captured is False
    assert outcome.skipped == "disabled"
    assert await _row_count() == 0


async def test_payload_is_stored_as_strict_json():
    """非 JSON 值（日期、UUID）要落成字符串，而不是让整次写入炸掉。

    备份是写操作的前置条件，这里抛异常等于把「记不下来」升级成「干不了」。
    """
    outcome = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="t",
        decision="approved",
        policy_revision=1,
        spec=BackupSpec(
            resource_type="workflow",
            resource_id=str(uuid.uuid4()),
            payload={"at": datetime.datetime(2026, 11, 23, 9, 0)},
        ),
    )

    assert outcome.captured is True
    assert outcome.size_bytes > 0


# ---------------------------------------------------------------- 保留期


async def _capture(blob: str, resource_id: str = "wf-1") -> None:
    await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="studio_workflow_save",
        decision="approved",
        policy_revision=1,
        spec=BackupSpec(
            resource_type="workflow", resource_id=resource_id, payload={"definition": blob}
        ),
    )


async def _payloads() -> list[str]:
    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(WriteBackup).order_by(WriteBackup.created_at.asc(), WriteBackup.id.asc())
            )
        ).all()
    return [row.payload["definition"] for row in rows]


async def _insert_old_backup(
    *,
    days: int = 0,
    minutes_ago: int = 0,
    size_bytes: int,
    resource_id: str = "wf-old",
    payload: str = "old",
) -> None:
    """直接插一行历史备份：`created_at` 要能指定，否则测不出「留最近几份」。"""
    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                WriteBackup(
                    tenant_id=TEST_TENANT,
                    tool_name="studio_workflow_save",
                    resource_type="workflow",
                    resource_id=resource_id,
                    decision="approved",
                    policy_revision=1,
                    payload={"definition": payload},
                    size_bytes=size_bytes,
                    created_at=datetime.datetime.now(datetime.UTC)
                    - datetime.timedelta(days=days, minutes=minutes_ago),
                )
            )


async def test_keeps_only_the_newest_snapshots_of_one_resource():
    """同一张流程改八次，只留最近五份。

    否则回滚永远读得到，但库会一直被同一张图的历代版本占着——而这正是
    「总量满了、新备份悄悄停掉」的成因。

    历史行直接插、时间戳显式错开：`created_at` 是库端 `now()`，在测试库里
    同一个事务内是同一个值，八行会并列——那时「哪五份算最近的」就不是这条
    规则能回答的问题（顺序退化成 uuid 排序）。
    """
    for index in range(7):
        await _insert_old_backup(
            days=0, size_bytes=1024, resource_id="wf-1", payload=f"v{index}",
            minutes_ago=(7 - index) * 10,
        )
    await _capture("v7")

    assert await _payloads() == [f"v{i}" for i in range(3, 8)]


async def test_retention_counts_per_resource_not_globally():
    """按资源计份：八张不同的流程各备一份，一份都不该被删。"""
    for index in range(8):
        await _capture(f"v{index}", resource_id=f"wf-{index}")

    assert await _row_count() == 8


async def test_drops_snapshots_older_than_the_age_limit():
    await _insert_old_backup(days=write_backup_service.MAX_AGE_DAYS + 10, size_bytes=1024)

    await _capture("fresh")

    payloads = await _payloads()
    assert payloads == ["fresh"]


async def test_pruning_frees_space_so_capture_does_not_stop_early():
    """总量满了时，先按保留期清掉过期的，再判能不能备。

    不清就判的话，一个三个月没动过的租户会一直 `store_full`——它的空间全被
    早该删掉的旧快照占着，而真正需要恢复的那一次落在「停止」之后。
    """
    await runtime_settings.set_backup_max_size_mb(1, actor="tester")
    await _insert_old_backup(
        days=write_backup_service.MAX_AGE_DAYS + 10, size_bytes=900 * 1024
    )

    outcome = await write_backup_service.capture(
        tenant_id=TEST_TENANT,
        tool_name="studio_workflow_save",
        decision="approved",
        policy_revision=1,
        spec=BackupSpec(
            resource_type="workflow", resource_id="wf-1", payload={"definition": "x" * 200_000}
        ),
    )

    assert outcome.captured is True
    assert outcome.skipped is None


async def test_repeated_captures_hold_at_the_retention_count():
    """连改八次之后库里正好留五份——不是六份。

    只裁「插之前」会稳定多留一份：8 行时裁到 5，插入变 6，下次再裁到 5 再插。
    稳态多出来的那一份与文档写的份数不符，而「留几份」是这条规则唯一的承诺。
    """
    for index in range(8):
        await _capture(f"v{index}")

    assert await _row_count() == write_backup_service.KEEP_PER_RESOURCE
