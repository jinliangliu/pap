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
