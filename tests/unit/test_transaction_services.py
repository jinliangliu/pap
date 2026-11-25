# tests/unit/test_transaction_services.py
"""事务层纯逻辑：cron 计算、快照轮转规则、状态机、cpu_ms 累加。

这里只测不碰库的确定性逻辑；涉及数据库与文件系统的部分在
test_transaction_integration.py。分开的原因：这批测试毫秒级，可以
在图里反复跑，不必每次都建临时目录和 session。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.task import TaskStatus
from app.services import snapshot_service as snapshots
from app.services import task_service as tasks
from app.services.schedule_service import (
    CronError,
    make_idempotency_key,
    next_fire_at,
    parse_cron,
)

# ---- cron -----------------------------------------------------------------

def test_cron_daily_at_local_time():
    """北京时间 8:00 = UTC 0:00；从当天 UTC 0:00 之后算，落在次日。"""
    result = next_fire_at(
        "0 8 * * *",
        after=datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
        timezone_offset_hours=8,
    )
    assert result == datetime(2026, 11, 26, 0, 0, tzinfo=UTC)


def test_cron_step_and_weekday_range():
    """每 5 分钟、仅工作日： Monday 00:00 -> 00:05。"""
    result = next_fire_at(
        "*/5 * * * 1-5",
        after=datetime(2026, 11, 23, 0, 1, tzinfo=UTC),  # 2026-11-23 是周一
        timezone_offset_hours=0,
    )
    assert result == datetime(2026, 11, 23, 0, 5, tzinfo=UTC)


def test_cron_rejects_bad_expression():
    with pytest.raises(CronError):
        parse_cron("* * * *")  # 少一段
    with pytest.raises(CronError):
        parse_cron("99 8 * * *")  # 分钟越界
    with pytest.raises(CronError):
        parse_cron("*/0 * * * *")  # 步长必须大于 0
    # 2 月 30 日是合法语法但五年内无匹配点，由 next_fire 的扫描上限兜住。
    with pytest.raises(CronError):
        next_fire_at("0 0 30 2 *", after=datetime(2026, 1, 1, tzinfo=UTC), timezone_offset_hours=0)


def test_idempotency_key_is_stable_per_fire():
    fire = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)
    sid = __import__("uuid").uuid4()
    assert make_idempotency_key(sid, fire) == make_idempotency_key(sid, fire)
    assert "20261125T000000Z" in make_idempotency_key(sid, fire)


# ---- 快照轮转 --------------------------------------------------------------

def _snap(sid: str, at: datetime) -> snapshots.SnapshotInfo:
    return snapshots.SnapshotInfo(
        snapshot_id=sid,
        path=__import__("pathlib").Path("/tmp") / sid,
        created_at=at,
    )


def test_retention_keeps_recent_plus_bucket_first(monkeypatch):
    """最近 N 次 ∪ 每半小时桶最早一次；同一桶里较晚的旧快照可以删。"""
    monkeypatch.setattr(snapshots.settings, "snapshot_keep_recent", 2)
    monkeypatch.setattr(snapshots.settings, "snapshot_bucket_minutes", 30)
    monkeypatch.setattr(snapshots.settings, "snapshot_max_buckets", 48)

    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    all_snaps = [
        _snap("old-0901", datetime(2026, 11, 25, 9, 1, tzinfo=UTC)),   # 桶 9:00 最早
        _snap("old-0920", datetime(2026, 11, 25, 9, 20, tzinfo=UTC)),  # 同桶较晚，可删
        _snap("r1-0940", datetime(2026, 11, 25, 9, 40, tzinfo=UTC)),   # 桶 9:30 最早
        _snap("r2-1000", datetime(2026, 11, 25, 10, 0, tzinfo=UTC)),   # 最近第 2
        _snap("r3-1010", datetime(2026, 11, 25, 10, 10, tzinfo=UTC)),  # 最近第 1
    ]
    retained = snapshots.select_retained(all_snaps, now=now)

    assert "old-0901" in retained   # 桶最早
    assert "old-0920" not in retained
    assert "r1-0940" in retained
    assert "r2-1000" in retained
    assert "r3-1010" in retained


def test_retention_caps_bucket_window(monkeypatch):
    """超过半小时桶上限（24h）的旧快照，即便某桶最早也不再保留。"""
    monkeypatch.setattr(snapshots.settings, "snapshot_keep_recent", 1)
    monkeypatch.setattr(snapshots.settings, "snapshot_bucket_minutes", 30)
    monkeypatch.setattr(snapshots.settings, "snapshot_max_buckets", 2)

    now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    ancient = _snap("ancient", datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
    recent = _snap("recent", datetime(2026, 11, 25, 9, 50, tzinfo=UTC))
    retained = snapshots.select_retained([ancient, recent], now=now)

    assert "ancient" not in retained
    assert "recent" in retained


# ---- 任务状态机 ------------------------------------------------------------

def test_transition_edges():
    assert tasks.can_transition(TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert tasks.can_transition(TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
    assert tasks.can_transition(TaskStatus.RUNNING, TaskStatus.UNKNOWN)
    # 终态无出边，非法跳转必须被拒。
    assert not tasks.can_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)
    assert not tasks.can_transition(TaskStatus.UNKNOWN, TaskStatus.QUEUED)
    assert not tasks.can_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED)


def test_cpu_ms_never_treats_missing_as_zero():
    # None 是「还没有有效累计」的初值，首次回报应当落值；
    # 一旦某次回报缺失（executed=None），累计值回到 None，不让部分里程冒充总里程。
    assert tasks.accumulate_cpu_ms(None, 10) == 10
    assert tasks.accumulate_cpu_ms(10, None) is None
    assert tasks.accumulate_cpu_ms(None, None) is None
    assert tasks.accumulate_cpu_ms(10, 0) == 10
    assert tasks.accumulate_cpu_ms(0, 0) == 0
    assert tasks.accumulate_cpu_ms(5, 7) == 12
