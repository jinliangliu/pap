# tests/unit/test_agent_loop.py
"""推理回路：工具结果回灌、收敛策略、兜底作答。

这里是「让模型自主决定何时调用工具」的核心约束，必须锁住：
- 成功一次后不能被直接吞掉结论；
- 失败不重试第二遍；
- 步数用尽必须收敛；
- 同参重放要短路。
"""

from __future__ import annotations

from app.agents import nodes
from app.core.config import settings


def test_route_without_observation_goes_to_respond():
    assert nodes.route_after_read_tool({}) == "respond"


def test_route_on_failure_goes_to_respond():
    state = {
        "tool_steps": 1,
        "tool_observations": [
            {"tool_name": "ocr_document", "success": False, "text": "失败", "round": 1}
        ],
    }
    assert nodes.route_after_read_tool(state) == "respond"


def test_route_on_success_loops_back_to_planner():
    state = {
        "tool_steps": 1,
        "tool_observations": [
            {"tool_name": "ocr_document", "success": True, "text": "文字", "round": 1}
        ],
    }
    assert nodes.route_after_read_tool(state) == "plan_request"


def test_route_stops_when_steps_exhausted():
    state = {
        "tool_steps": settings.agent_max_tool_steps,
        "tool_observations": [
            {
                "tool_name": "ocr_document",
                "success": True,
                "text": "文字",
                "round": settings.agent_max_tool_steps,
            }
        ],
    }
    assert nodes.route_after_read_tool(state) == "respond"


def test_route_when_round_not_written_goes_to_respond():
    """本轮没记账时必须收尾，不能靠上一轮的成功记录继续。

    2026-11-04 的线上死循环就是这个状态：observations 里留着上一轮的成功结果，
    tool_steps 也没涨，路由于是永远判定「可以继续」，plan ↔ tool 空转 45 分钟。
    现在 observation 带 round，与步数对不上就说明这一轮没落地。
    """
    state = {
        "tool_steps": 1,
        "tool_observations": [{"tool_name": "ocr_document", "success": True, "round": 0}],
    }
    assert nodes.route_after_read_tool(state) == "respond"


class _RejectingGateway:
    """网关前置校验就拒绝：模拟参数不合法 / 未注册 / 被限流。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def execute_read_only(self, **_kwargs):
        raise self._exc


async def test_execute_read_tool_failure_still_counts_a_step(monkeypatch):
    """失败也必须留下 observation 并推进步数。

    否则 route_after_read_tool 读到的是上一轮的成功记录，回路不收敛。
    """
    monkeypatch.setattr(
        nodes,
        "get_tool_gateway",
        lambda: _RejectingGateway(nodes.ToolGatewayError("请求的工具未注册：ocr_document")),
    )
    state = {
        "tenant_id": "default",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "planned_tool_name": "ocr_document",
        "planned_arguments": {"file_id": "img_0123456789ab"},
        "tool_steps": 1,
        # 上一轮成功过，这正是「读到旧记录」的条件。
        "tool_observations": [
            {"tool_name": "ocr_document", "success": True, "text": "文字", "round": 1}
        ],
    }

    result = await nodes.execute_read_tool(state)

    assert result["tool_steps"] == 2
    assert result["error_category"] == "tool_rejected"
    assert result["tool_observations"][-1]["success"] is False
    assert result["tool_observations"][-1]["round"] == 2
    # 合并后必须是「收尾」而不是「回规划器」。
    assert nodes.route_after_read_tool({**state, **result}) == "respond"


async def test_execute_read_tool_without_planned_tool_counts_a_step(monkeypatch):
    """计划器没给出工具名也要记账，否则同样会空转。"""
    monkeypatch.setattr(nodes, "get_tool_gateway", lambda: _RejectingGateway(RuntimeError()))

    result = await nodes.execute_read_tool(
        {
            "tenant_id": "default",
            "user_id": "user-1",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "tool_steps": 0,
            "tool_observations": [],
        }
    )

    assert result["tool_steps"] == 1
    assert result["tool_observations"][-1]["success"] is False
    assert result["error_category"] == "tool_not_planned"


def test_render_observations_includes_tool_output_and_next_step_hint():
    text = nodes._render_observations(
        {
            "tool_observations": [
                {"tool_name": "ocr_document", "success": True, "text": "站点 T002509008"},
                {"tool_name": "prv_pressure_trend", "success": False, "text": "超时"},
            ]
        }
    )

    assert "[本轮工具结果]" in text
    assert "站点 T002509008" in text
    assert "prv_pressure_trend 调用失败" in text
    # 必须提示模型「够了就作答」，否则容易在回路里空转
    assert "action=chat" in text


def test_render_observations_empty_when_no_tool_ran():
    assert nodes._render_observations({}) == ""


def test_repeated_call_with_same_arguments_is_detected():
    state = {
        "tool_observations": [
            {
                "tool_name": "ocr_document",
                "arguments": {"file_id": "img_0123456789ab"},
                "success": True,
            }
        ]
    }

    assert nodes._repeats_last_observation(
        state, tool_name="ocr_document", arguments={"file_id": "img_0123456789ab"}
    )
    assert not nodes._repeats_last_observation(
        state, tool_name="ocr_document", arguments={"file_id": "img_ffffffffffff"}
    )
    assert not nodes._repeats_last_observation(
        state, tool_name="prv_pressure_trend", arguments={"file_id": "img_0123456789ab"}
    )


async def test_respond_falls_back_to_last_observation():
    result = await nodes.respond(
        {"tool_observations": [{"tool_name": "ocr_document", "text": "识别出的文字"}]}
    )
    assert result["final_response"] == "识别出的文字"


async def test_respond_prefers_final_response():
    result = await nodes.respond(
        {
            "final_response": "模型作答",
            "tool_observations": [{"tool_name": "ocr_document", "text": "识别出的文字"}],
        }
    )
    assert result["final_response"] == "模型作答"
