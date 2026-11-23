# tests/unit/test_runtime_view.py
"""运行可视化 V1 的数据基座。

三条必须成立的性质，每条都能被反向改动打红：

1. **归属正确**：委派子图里的 span 归被委派方，委派那一层自己也归被委派方；
2. **口径守恒**：加了 `operation_id` 之后，「按 agent 求和 == 总数」仍然成立；
3. **状态不打架**：三套词表压出来的 `display_state` 顺序不能反——
   失败优先于等待，否决优先于失败。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.observability import context as obs_context
from app.observability.context import (
    bind_agent,
    current_agent,
    current_operation_id,
    span,
    trace_run,
)
from app.observability.state import display_state, needs_attention


@pytest.fixture
def collected(monkeypatch):
    """把 trace 收尾时的提交拦在内存里，不碰数据库。

    只收 `type="finish"`：`trace_run` 现在会先提交一条 `type="start"` 的起始行
    （运行视图靠它回答「谁在跑」）。那份在这里没有断言价值，混进来却会让
    「第 0 条 / 第 1 条是哪一轮」全部错位。起始行本身在
    `tests/integration/test_obs_live.py` 里用真实写入验证。
    """
    boxes: list[dict] = []

    def _collect(bundle: dict) -> None:
        if bundle.get("type") == "finish":
            boxes.append(bundle)

    monkeypatch.setattr(obs_context, "_submit", _collect)
    monkeypatch.setattr(settings, "obs_enabled", True)
    return boxes


def _spans(bundle: dict) -> dict[str, dict]:
    return {row["name"]: row for row in bundle["spans"]}


# ---------------------------------------------------------------- 归属


@pytest.mark.asyncio
async def test_delegated_subgraph_belongs_to_the_callee(collected):
    """A→B：B 的节点归 B，A 自己的节点归 A。"""
    async with trace_run(tenant_id="t1", user_id="u1", agent_name="cs") as _trace:
        async with span("node.plan_request", "node") as caller_plan:
            caller_plan.add(model="x")
        # 委派：bind_agent 套在 span 外面，所以 agent.ops 这一层也归 ops。
        with bind_agent("ops"):
            async with span("agent.ops", "agent") as agent_span:
                agent_span.add(depth=1)
                async with span("node.plan_request", "node"):
                    pass

    rows = collected[-1]["spans"]
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)

    caller, callee = by_name["node.plan_request"]
    assert caller["agent_name"] == "cs"
    assert callee["agent_name"] == "ops"
    # 委派这一层自己也必须归被委派方，否则拓扑图里这条边会指回发起方。
    assert by_name["agent.ops"][0]["agent_name"] == "ops"


@pytest.mark.asyncio
async def test_nested_delegation_restores_the_caller(collected):
    """A→B→C：C 归 C，回到 B 之后 B 的节点仍然归 B。"""
    async with trace_run(tenant_id="t1", user_id="u1", agent_name="cs"):
        with bind_agent("ops"):
            async with span("agent.ops", "agent"):
                with bind_agent("audit"):
                    async with span("agent.audit", "agent"):
                        async with span("node.plan_request", "node"):
                            pass
                async with span("node.respond", "node"):
                    pass

    by_name: dict[str, list[dict]] = {}
    for row in collected[-1]["spans"]:
        by_name.setdefault(row["name"], []).append(row)

    assert by_name["agent.ops"][0]["agent_name"] == "ops"
    assert by_name["agent.audit"][0]["agent_name"] == "audit"
    assert by_name["node.plan_request"][0]["agent_name"] == "audit"
    # 退出内层委派后必须还原，否则同一轮里后面的节点会全部错挂到 audit 上。
    assert by_name["node.respond"][0]["agent_name"] == "ops"


@pytest.mark.asyncio
async def test_bind_agent_is_restored_after_the_block():
    with bind_agent("ops"):
        assert current_agent() == "ops"
    assert current_agent() is None


# ---------------------------------------------------------------- 运行键


@pytest.mark.asyncio
async def test_operation_id_defaults_to_own_trace_id(collected):
    """第一轮发起时没有可继承的值，于是自己就是一次运行的起点。"""
    async with trace_run(tenant_id="t1", user_id="u1") as trace:
        assert trace is not None
        assert current_operation_id() == trace.trace_id

    assert collected[-1]["trace"]["operation_id"] == collected[-1]["trace"]["trace_id"]


@pytest.mark.asyncio
async def test_operation_id_is_inherited_on_resume(collected):
    """审批确认后的那一轮继承发起方的运行键，两条 trace 因此是同一次运行。"""
    async with trace_run(tenant_id="t1", user_id="u1") as first:
        assert first is not None
        root = current_operation_id()

    async with trace_run(tenant_id="t1", user_id="u1", operation_id=root) as second:
        assert second is not None
        # 是新 trace，但不是新运行。
        assert second.trace_id != first.trace_id
        assert current_operation_id() == root

    assert collected[0]["trace"]["operation_id"] == collected[1]["trace"]["operation_id"]
    assert collected[0]["trace"]["trace_id"] != collected[1]["trace"]["trace_id"]


@pytest.mark.asyncio
async def test_operation_id_is_absent_when_observation_is_off(monkeypatch):
    """关掉观测时整条链路都是空转，调用方拿到的 None 不需要特判。"""
    monkeypatch.setattr(settings, "obs_enabled", False)
    async with trace_run(tenant_id="t1", user_id="u1") as trace:
        assert trace is None
        assert current_operation_id() is None


# ---------------------------------------------------------------- 状态派生


def test_pending_approval_is_input_required():
    """等人不是失败，也不是完成——A2A 的 input-required 就是这个意思。"""
    assert (
        display_state(trace_status="awaiting_approval", approval_status="PENDING")
        == "input-required"
    )


def test_failure_beats_waiting():
    """子调用崩在审批之后：库里同时有「trace 已 error」和「单子还 PENDING」。

    显示成 input-required 会让人以为「点一下确认就好了」，而没有人会去恢复它。
    """
    assert display_state(trace_status="error", approval_status="PENDING") == "failed"
    assert display_state(trace_status="timeout", approval_status="PENDING") == "failed"


def test_revoked_beats_failed():
    """批准期间上游收窄：B 可能完全健康，显示成 failed 会把人引去查 B。"""
    assert (
        display_state(
            trace_status="error",
            approval_status="REJECTED",
            error_category="DELEGATION_REVOKED",
        )
        == "rejected"
    )


def test_unknown_outcome_is_failed_not_completed():
    """「外部可能已经写了，但确认不了」必须落到需要处理的一类，不能算完成。"""
    assert display_state(trace_status="ok", approval_status="EXECUTION_UNKNOWN") == "failed"
    assert display_state(trace_status="ok", approval_status="RECOVERY_REQUIRED") == "failed"


def test_cancel_and_expiry_are_canceled_not_failed():
    assert display_state(trace_status="cancelled") == "canceled"
    assert display_state(trace_status="ok", approval_status="EXPIRED") == "canceled"
    assert display_state(trace_status="ok", approval_status="REJECTED") == "canceled"


def test_plain_success_is_completed():
    assert display_state(trace_status="ok") == "completed"
    # EXECUTED 比 VERIFIED 弱一档，但都属于「做完了」，强弱另有一列标注。
    assert display_state(trace_status="ok", approval_status="EXECUTED") == "completed"
    assert display_state(trace_status="ok", approval_status="VERIFIED") == "completed"


def test_finished_trace_with_a_live_ticket_needs_attention():
    """运行已终态但单子还挂着：列表里看着「完成」，实际留了一张没人管的单。"""
    assert (
        needs_attention(
            display_state_value="failed",
            approval_status="PENDING",
            trace_status="error",
        )
        is True
    )
    # 正常在等的不算「需要处理」，那就是它的正常状态。
    assert (
        needs_attention(
            display_state_value="input-required",
            approval_status="PENDING",
            trace_status="awaiting_approval",
        )
        is False
    )
