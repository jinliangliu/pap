# tests/unit/test_agent_turn.py
"""每轮开始必须清掉上一轮的单轮状态。

LangGraph 会把 checkpoint 里「本轮输入没有提到」的通道原样带进新一轮，
而 chat_service 只提交了消息、附件与身份。于是上一轮的工具结果、步数、
计划参数会全部沿用——2026-11-05 线上「先传图片、再传 PDF，模型把旧图片的
识别结果当成新 PDF 的内容」就是这个原因。
"""

from __future__ import annotations

from app.agents import nodes


async def test_begin_turn_clears_previous_turn_state():
    state = {
        "intent": "read_tool",
        "tool_observations": [
            {
                "tool_name": "ocr_document",
                "arguments": {"file_id": "img_26c228691055"},
                "success": True,
                "text": "调压箱/柜现场记录表 出口压力 2.3kPa",
                "round": 1,
            }
        ],
        "tool_steps": 2,
        "final_response": "上一轮的答案",
        "planned_tool_name": "ocr_document",
        "planned_arguments": {"file_id": "img_26c228691055"},
        "planned_reason": "需要识别附件",
        "error_category": "tool_failed",
        "approval_id": "appr-1",
        "approval_preview": {"summary": "上一轮的审批预览"},
        "execution_status": "PENDING",
        "missing_fields": [],
    }

    reset = await nodes.begin_turn(state)

    assert reset["tool_observations"] == []
    assert reset["tool_steps"] == 0
    assert reset["final_response"] is None
    assert reset["planned_tool_name"] is None
    assert reset["planned_arguments"] is None
    assert reset["planned_reason"] is None
    assert reset["approval_id"] is None
    assert reset["approval_preview"] is None
    assert reset["execution_status"] is None
    assert reset["error_category"] is None
    assert reset["intent"] is None


async def test_begin_turn_keeps_slots_when_previous_turn_asked_for_them():
    """上一轮以「缺槽位」收尾时保留工具与已确认参数：多轮补参靠它。"""
    state = {
        "intent": "clarify",
        "missing_fields": ["station_id"],
        "planned_tool_name": "prv_query_pressure",
        "planned_arguments": {"lang": "zh"},
        "tool_observations": [],
        "tool_steps": 0,
    }

    reset = await nodes.begin_turn(state)

    assert reset["planned_tool_name"] == "prv_query_pressure"
    assert reset["planned_arguments"] == {"lang": "zh"}
    # 补参轮并没有执行过工具，观察与步数照常归零
    assert reset["tool_observations"] == []
    assert reset["tool_steps"] == 0
    assert reset["missing_fields"] == []
    assert reset["final_response"] is None


async def test_begin_turn_on_fresh_state_is_empty_but_complete():
    """第一轮没有历史状态：返回值仍然给出全部键，覆盖语义才成立。"""
    reset = await nodes.begin_turn({"tenant_id": "default"})

    for key in nodes._PER_TURN_FIELDS:
        assert key in reset

    assert reset["tool_observations"] == []
    assert reset["missing_fields"] == []


async def test_begin_turn_returns_independent_lists():
    """两轮的观测列表不能是同一个对象，否则一轮的写入会污染另一轮。"""
    first = await nodes.begin_turn({})
    second = await nodes.begin_turn({})

    first["tool_observations"].append({"tool_name": "x"})

    assert second["tool_observations"] == []
    assert nodes._PER_TURN_FIELDS["tool_observations"] == []


def test_run_status_follows_the_approval_outcome():
    """运行的终态必须跟审批结论一致，不能一律记完成。

    原先只判断 `PENDING`，其余全 COMPLETED——工具执行失败与用户取消都被记成
    完成，而且是**朝好看的方向错**：失败率与 p95 都不会报警。
    """
    from app.models.conversation import RunStatus

    assert nodes.run_status_for({"intent": "read_tool"}) is RunStatus.COMPLETED
    assert nodes.run_status_for({"intent": "error"}) is RunStatus.FAILED

    # 停在等审批
    assert (
        nodes.run_status_for({"intent": "write_tool", "execution_status": "PENDING"})
        is RunStatus.WAITING_FOR_APPROVAL
    )

    # 执行成功的两种：核验通过 / 仅提交
    for ok in ("VERIFIED", "EXECUTED"):
        assert (
            nodes.run_status_for({"intent": "write_tool", "execution_status": ok})
            is RunStatus.COMPLETED
        )

    # 执行失败的三类
    for bad in ("failed", "FAILED", "EXECUTION_UNKNOWN", "RECOVERY_REQUIRED"):
        assert (
            nodes.run_status_for({"intent": "write_tool", "execution_status": bad})
            is RunStatus.FAILED
        ), bad

    # 没执行成的两类：取消与过期
    for stopped in ("rejected", "EXPIRED"):
        assert (
            nodes.run_status_for({"intent": "write_tool", "execution_status": stopped})
            is RunStatus.CANCELLED
        ), stopped


def test_expired_approval_is_cancelled_not_failed():
    """过期不是执行失败：这次写入从未发生，原因也不在工具。

    走 FAILED 会和「工具报错」「外部系统拒绝」混进同一个桶，把「没做」
    与「做了但没成」抹平。
    """
    import uuid as _uuid

    from app.agents.nodes import _approval_failure

    expired = _approval_failure(
        _uuid.uuid4(), "预览已过期", "approval_expired", execution_status="EXPIRED"
    )

    assert expired["execution_status"] == "EXPIRED"
    assert nodes.run_status_for(expired).value == "CANCELLED"
