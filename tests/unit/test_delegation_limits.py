# tests/unit/test_delegation_limits.py
"""委派预算：默认值、沿链取最严、以及「0 不是没配」。

这个模块的风险不在算术，在语义。三个坑各有一条用例守着：

1. 用 `x or default` 折配置 → 配成 0（不许转交）会被当成没配，开关静默失效；
2. 下游把上限调大 → 整条链被链中间的某一环单方面放宽；
3. checkpoint 里的脏值 → 一个诊断字段让整轮对话打不开。

纯逻辑，不碰数据库：这套规则同时被图入口、工具执行、子调用三处使用，
放在集成测试里测会慢到没人愿意跑，而它恰恰是最该被频繁验证的一段。
"""

from __future__ import annotations

import pytest

from app.services.delegation_limits import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PER_TURN,
    MAX_DEPTH_CEILING,
    MAX_PER_TURN_CEILING,
    DelegationLimits,
    check_limit,
)


def test_defaults_are_the_platform_baseline():
    limits = DelegationLimits()
    assert limits.max_depth == DEFAULT_MAX_DEPTH
    assert limits.max_per_turn == DEFAULT_MAX_PER_TURN


def test_narrowing_takes_the_stricter_side():
    limits = DelegationLimits(max_depth=3, max_per_turn=5).narrowed_by(
        max_depth=1, budget=2
    )
    assert (limits.max_depth, limits.max_per_turn) == (1, 2)


def test_narrowing_cannot_widen():
    """下游配得比上游宽松时，以上游为准。

    这是「配置只收窄」在委派预算上的落点：B 把深度调成 5，不能让 A 已经
    收窄到 1 的那条链跑到 5 跳——A 从来没有同意过这个深度。
    """
    limits = DelegationLimits(max_depth=1, max_per_turn=1).narrowed_by(
        max_depth=5, budget=10
    )
    assert (limits.max_depth, limits.max_per_turn) == (1, 1)


def test_unset_keeps_the_inherited_value():
    limits = DelegationLimits(max_depth=1, max_per_turn=2).narrowed_by()
    assert (limits.max_depth, limits.max_per_turn) == (1, 2)


def test_zero_is_a_value_not_a_missing_one():
    """0 = 一次都不许。写成 `x or default` 会让它变成「没配」。"""
    limits = DelegationLimits().narrowed_by(max_depth=0, budget=0)
    assert (limits.max_depth, limits.max_per_turn) == (0, 0)


def test_state_round_trip():
    limits = DelegationLimits(max_depth=4, max_per_turn=7)
    assert DelegationLimits.from_state(limits.as_state()) == limits


def test_state_tolerates_garbage():
    """旧 checkpoint / 手改过的值不该让整轮对话打不开。"""
    assert DelegationLimits.from_state(None) == DelegationLimits()
    assert DelegationLimits.from_state("nope") == DelegationLimits()
    assert DelegationLimits.from_state({}) == DelegationLimits()
    # 负数夹到 0，超天花板夹到天花板：方向永远是「不放大」。
    assert DelegationLimits.from_state({"max_depth": -3}).max_depth == 0
    assert DelegationLimits.from_state({"max_depth": 99}).max_depth == MAX_DEPTH_CEILING
    assert (
        DelegationLimits.from_state({"max_per_turn": 999}).max_per_turn
        == MAX_PER_TURN_CEILING
    )


def test_refusal_copy_separates_zero_from_a_limit():
    """配成 0 与「到了上限」是两件事，说成「最多 0 跳」只会让人去查配置。"""
    assert "不转交" in DelegationLimits(max_depth=0).depth_refusal()
    assert "最多 2 跳" in DelegationLimits(max_depth=2).depth_refusal()
    assert "不转交" in DelegationLimits(max_per_turn=0).budget_refusal()
    assert "3 次" in DelegationLimits(max_per_turn=3).budget_refusal()


def test_check_limit_keeps_null_and_zero_apart():
    assert check_limit("上限", None, ceiling=MAX_DEPTH_CEILING) is None
    assert check_limit("上限", 0, ceiling=MAX_DEPTH_CEILING) == 0


@pytest.mark.parametrize(
    ("value", "ceiling", "fragment"),
    [
        (-1, MAX_DEPTH_CEILING, "不能是负数"),
        (MAX_DEPTH_CEILING + 1, MAX_DEPTH_CEILING, "最多"),
        (MAX_PER_TURN_CEILING + 1, MAX_PER_TURN_CEILING, "最多"),
    ],
)
def test_check_limit_rejects_out_of_range(value, ceiling, fragment):
    with pytest.raises(ValueError) as exc:
        check_limit("上限", value, ceiling=ceiling)
    assert fragment in str(exc.value)

