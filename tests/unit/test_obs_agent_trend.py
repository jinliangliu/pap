# tests/unit/test_obs_agent_trend.py
"""按智能体的趋势：分桶与「能不能比」的判定。

这一层的风险不在算错，而在**算出一个看起来正常、其实没意义的数**：

- 上一窗口是 0 轮，除不出比例 → 不能画成 0% 或 +100%；
- Agent 是在上一窗口中途建的，那一段覆盖不完整 → 不能当基线；
- 2 轮 → 3 轮，也只是同一个人的两次重试 → 比例是噪声。

所以 `change=None` 一定要带原因。这三个原因的分支各自钉一条用例。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api.routes.observability import (
    BUCKET_LADDER,
    MAX_BUCKETS,
    TREND_MIN_SAMPLE,
    _bucket_edges,
    _bucket_index,
    _change,
    _pick_bucket_hours,
)
from app.schemas.observability import ObsAgentStat

END = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (1, 1),
        (24, 1),  # 24 格 × 1h，还在可读范围内
        (48, 3),  # 48 格太密，退到 3h × 16
        (96, 6),
        (168, 6),  # 一周 = 28 格 × 6h，正好压在上限
        (336, 12),  # 上限 14 天
    ],
)
def test_bucket_ladder_keeps_series_readable(hours, expected):
    assert _pick_bucket_hours(hours) == expected
    # 无论选到哪一档，格数都不该超过上限。
    assert hours / _pick_bucket_hours(hours) <= MAX_BUCKETS


def test_bucket_ladder_covers_the_longest_window():
    """最长窗口（14 天）要落在阶梯的某一档上，而不是落到兜底值。"""
    assert _pick_bucket_hours(24 * 14) == 12
    assert _pick_bucket_hours(24 * 14) in BUCKET_LADDER


def test_edges_end_exactly_now():
    """最后一格收在「现在」——不是收在某个整点。

    按整点（自然日）对齐，会让最新一格是半格：上午十点看「今天」永远比昨天少，
    而那不是用量在跌。
    """
    start = END - timedelta(hours=24)
    edges = _bucket_edges(start=start, end=END, bucket_hours=1)

    assert len(edges) == 24
    assert edges[-1] == END
    assert edges[0] == END - timedelta(hours=23)
    # 等距（升序）。
    assert all(b - a == timedelta(hours=1) for a, b in zip(edges, edges[1:], strict=False))


def test_edges_flag_the_clipped_oldest_bucket():
    """窗口长度不是桶宽整数倍时，最旧一格会被起点截断，必须能标出来。"""
    start = END - timedelta(hours=50)
    edges = _bucket_edges(start=start, end=END, bucket_hours=3)
    width = timedelta(hours=3)

    assert len(edges) == 17  # ceil(50 / 3)
    assert edges[-1] == END
    # 最旧一格起点早于窗口起点 → 这一格只覆盖了一部分时间。
    assert edges[0] - width < start
    # 第二旧的一格是完全落在窗口内的。
    assert edges[1] - width >= start


def test_bucket_index_is_zero_for_the_newest_slot():
    assert _bucket_index(ts=END - timedelta(minutes=1), end=END, bucket_hours=1) == 0
    assert _bucket_index(ts=END - timedelta(hours=3), end=END, bucket_hours=1) == 3
    # 越界的返回负数，调用方据此丢弃（窗口边界与桶边界不整除时的零头）。
    assert _bucket_index(ts=END - timedelta(hours=30), end=END, bucket_hours=1) == 30


def test_change_is_plain_ratio_when_both_sides_have_enough():
    change, reason = _change(
        current=10,
        previous=5,
        created_at=END - timedelta(days=60),
        prev_start=END - timedelta(days=14),
    )
    assert (change, reason) == (1.0, None)

    change, reason = _change(
        current=6,
        previous=12,
        created_at=END - timedelta(days=60),
        prev_start=END - timedelta(days=14),
    )
    assert (change, reason) == (-0.5, None)


def test_change_refuses_to_divide_by_zero():
    """0 → 10 不该显示成 +100%：没有基线，就没有「涨了多少」。"""
    change, reason = _change(
        current=10,
        previous=0,
        created_at=END - timedelta(days=60),
        prev_start=END - timedelta(days=14),
    )
    assert change is None
    assert reason == "no_baseline"


def test_change_refuses_low_sample():
    change, reason = _change(
        current=3,
        previous=2,
        created_at=END - timedelta(days=60),
        prev_start=END - timedelta(days=14),
    )
    assert change is None
    assert reason == "low_sample"

    # 只要有一侧不足就成立——上一窗口 100 轮、这一窗口 2 轮
    # 看上去是「暴跌 98%」，实际是这一周还没人用。
    change, reason = _change(
        current=TREND_MIN_SAMPLE - 1,
        previous=100,
        created_at=END - timedelta(days=60),
        prev_start=END - timedelta(days=0),
    )
    assert change is None
    assert reason == "low_sample"


def test_change_marks_agent_created_inside_previous_window():
    """上一窗口中途才建的 Agent，那一段覆盖不完整——不能当基线。

    这条比 `no_baseline` 更靠前：新建的 Agent 上一窗口通常是 0，但如果只看
    「0 就不能比」，就永远说不清新 Agent 到底是「刚起步」还是「被放弃了」。
    """
    prev_start = END - timedelta(days=14)
    change, reason = _change(
        current=10,
        previous=3,
        created_at=prev_start + timedelta(days=12),  # 上一窗口快结束时才建
        prev_start=prev_start,
    )
    assert change is None
    assert reason == "created_after_baseline_start"


def test_change_allows_agent_created_before_previous_window():
    prev_start = END - timedelta(days=14)
    change, reason = _change(
        current=10,
        previous=5,
        created_at=prev_start - timedelta(seconds=1),
        prev_start=prev_start,
    )
    assert (change, reason) == (1.0, None)


def test_change_without_definition_falls_back_to_sample_rules():
    """Agent 已删除时拿不到 `created_at`，只能按样本量判断——不能因此就说不能比。"""
    change, reason = _change(
        current=10,
        previous=5,
        created_at=None,
        prev_start=END - timedelta(days=14),
    )
    assert (change, reason) == (1.0, None)


def test_unknown_change_reason_is_rejected():
    """`change_reason` 是封闭集合：多一个取值会静默变成空白提示，必须被拦下。"""
    for reason in ("no_baseline", "created_after_baseline_start", "low_sample"):
        assert ObsAgentStat(agent_name="a", change_reason=reason).change_reason == reason

    with pytest.raises(ValidationError):
        ObsAgentStat(agent_name="a", change_reason="whatever")
