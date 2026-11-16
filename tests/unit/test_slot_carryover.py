# tests/unit/test_slot_carryover.py
"""换工具时不继承上一个工具的参数。

回归 2026-11-04 线上事故的直接触发点：OCR 那轮的 `file_id` 被合并进站点查询工具，
网关报「Additional properties are not allowed ('file_id' was unexpected)」，
于是带图提问的第二轮**必然**失败。叠加当时「失败不记账」的回路缺陷，
就变成了 45 分钟空转。

同一工具的多轮补参仍要沿用已确认的参数——这是本次修改不能破坏的既有行为。
"""

from __future__ import annotations

from app.agents import nodes
from app.agents.schemas import ActionPlan


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
        self.input_schema = {
            "type": "object",
            "properties": {
                "station_id": {"type": "string"},
                "file_id": {"type": "string"},
                "lang": {"type": "string"},
            },
            "required": ["station_id"],
            "additionalProperties": False,
        }


class _FakeGateway:
    async def refresh_tenant_policy(self, _tenant_id: str) -> None:
        return None

    def get_tool(self, name: str) -> _FakeTool:
        return _FakeTool(name)

    def requires_approval(self, _tool: _FakeTool) -> bool:
        return False

    def requires_approval_for(self, *, tool: _FakeTool, context) -> bool:
        # 产品侧改成带上下文的判定后，替身也要跟着有：这里只关心槽位继承，
        # 审批与否不影响用例，返回 False 让流程走只读分支。
        return False


def _state(**overrides):
    state = {
        "tenant_id": "default",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "messages": [],
        "scopes": [],
        "roles": [],
        "tool_observations": [],
        "tool_steps": 0,
    }
    state.update(overrides)
    return state


def _install(monkeypatch, plan: ActionPlan) -> None:
    monkeypatch.setattr(nodes, "get_tool_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(nodes, "build_model_tool_catalog", lambda **_kwargs: [])

    async def _plan(**_kwargs) -> ActionPlan:
        return plan

    monkeypatch.setattr(nodes, "create_action_plan", _plan)


async def test_switching_tool_drops_previous_arguments(monkeypatch):
    _install(
        monkeypatch,
        ActionPlan(
            action="read_tool",
            tool_name="get_station_snapshot",
            arguments={"station_id": "station-001"},
            reason="OCR 已给出站点编号，接着查压力。",
        ),
    )

    result = await nodes.plan_request(
        _state(
            planned_tool_name="ocr_document",
            planned_arguments={"file_id": "img_0123456789ab"},
            tool_observations=[
                {
                    "tool_name": "ocr_document",
                    "success": True,
                    "text": "Station: T002509008",
                    "round": 1,
                }
            ],
            tool_steps=1,
        )
    )

    assert result["planned_tool_name"] == "get_station_snapshot"
    assert result["planned_arguments"] == {"station_id": "station-001"}
    assert "file_id" not in result["planned_arguments"]


async def test_same_tool_keeps_collected_arguments(monkeypatch):
    """同一工具补参：已确认的 file_id 必须保留，否则模型每轮都要重给。"""
    _install(
        monkeypatch,
        ActionPlan(
            action="read_tool",
            tool_name="ocr_document",
            arguments={"lang": "ch"},
            reason="用户补充了语言。",
        ),
    )

    result = await nodes.plan_request(
        _state(
            planned_tool_name="ocr_document",
            planned_arguments={"file_id": "img_0123456789ab"},
            tool_observations=[],
            tool_steps=0,
        )
    )

    assert result["planned_arguments"] == {
        "file_id": "img_0123456789ab",
        "lang": "ch",
    }
