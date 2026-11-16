# tests/unit/test_clarify.py
"""追问选项的规范化与出口。

这些用例保护的是「模型输出直接进界面」这条路上的三道闸：
条数、长度、字段白名单。它们错了不会抛异常，只会让某个用户的会话里
多出一张四十个选项的卡片，或者一行永远截不断的字。
"""

from __future__ import annotations

from app.agents import nodes
from app.agents.clarify import (
    MAX_ITEMS,
    MAX_LABEL_CHARS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    has_choices,
    normalize_items,
)


def test_normalize_keeps_valid_items():
    out = normalize_items(
        [
            {
                "question": "这句话要用在哪？",
                "field": "purpose",
                "options": [
                    {"label": "维护工单", "value": "ticket"},
                    {"label": "停机说明", "value": "notice"},
                ],
            }
        ],
        allowed_fields=["purpose"],
    )

    assert out == [
        {
            "question": "这句话要用在哪？",
            "field": "purpose",
            "options": [
                {"label": "维护工单", "value": "ticket"},
                {"label": "停机说明", "value": "notice"},
            ],
        }
    ]


def test_normalize_caps_items_and_options():
    items = [{"question": f"问题{i}", "options": [{"label": f"选项{j}"} for j in range(20)]} for i in range(10)]
    out = normalize_items(items)

    assert len(out) == MAX_ITEMS
    assert all(len(item["options"]) == MAX_OPTIONS for item in out)


def test_normalize_truncates_instead_of_dropping():
    """超长内容截断而不是整项丢掉：用户至少还能看到问的是哪一类事。"""
    out = normalize_items(
        [{"question": "问" * 1000, "options": [{"label": "选" * 500}]}]
    )

    assert len(out) == 1
    assert len(out[0]["question"]) == MAX_QUESTION_CHARS
    assert len(out[0]["options"][0]["label"]) == MAX_LABEL_CHARS


def test_normalize_drops_blank_and_duplicate_questions():
    out = normalize_items(
        [
            {"question": "   "},
            {"question": "同一个"},
            {"question": " 同一个 "},
            {},
            "不是对象的问题",
        ]
    )

    assert [item["question"] for item in out] == ["同一个", "不是对象的问题"]


def test_normalize_fills_value_from_label():
    """value 缺省即 label：模型经常只给 label，界面不该因此拿到空答案。"""
    out = normalize_items([{"question": "q", "options": [{"label": "高"}]}])

    assert out[0]["options"][0] == {"label": "高", "value": "高"}


def test_normalize_clears_field_outside_whitelist():
    """field 只用于标注「这项在补哪个槽」，不在本轮缺失列表里就清掉。"""
    out = normalize_items(
        [{"question": "哪个站？", "field": "station_id"}],
        allowed_fields=["severity"],
    )

    assert out[0]["field"] is None


def test_normalize_accepts_empty_allowed_fields_as_strict():
    """空白名单是「一个都不认」，不是「不校验」——后者会让乱填的 field 落库。"""
    out = normalize_items([{"question": "q", "field": "station_id"}], allowed_fields=[])

    assert out[0]["field"] is None


def test_normalize_tolerates_non_list():
    assert normalize_items(None) == []
    assert normalize_items({"question": "q"}) == []


def test_normalize_accepts_pydantic_items():
    """节点传进来的是 `ActionPlan` 里的 pydantic 对象，不是 dict。

    只认 dict 的写法会把每一项都当「非字符串」静默丢掉：正文说「请在下面选」，
    卡片却一张都没有，而且不报错。
    """
    from app.agents.schemas import ClarifyItem, ClarifyOption

    out = normalize_items(
        [ClarifyItem(question="用在哪？", options=[ClarifyOption(label="工单")])]
    )

    assert out[0]["question"] == "用在哪？"
    assert out[0]["options"][0] == {"label": "工单", "value": "工单"}


def test_clarify_item_is_constructible_without_any_field():
    """模型漏字段时不能整份 ActionPlan 校验失败——缺的留给 normalize 丢。"""
    from app.agents.schemas import ActionPlan, ClarifyItem, ClarifyOption

    plan = ActionPlan(
        action="clarify",
        clarify_items=[ClarifyItem(), ClarifyItem(question="q", options=[ClarifyOption()])],
    )

    assert len(plan.clarify_items) == 2
    # 全空的那项被丢掉，有问题的留下（候选为空 = 留给用户手填）。
    out = normalize_items(plan.clarify_items)
    assert [item["question"] for item in out] == ["q"]
    assert out[0]["options"] == []


def test_has_choices_only_when_some_item_offers_options():
    assert has_choices([{"question": "q", "options": [{"label": "a", "value": "a"}]}])
    assert not has_choices([{"question": "q", "options": []}])
    assert not has_choices([])


async def test_answer_carries_clarify_items_only_when_present():
    with_items = nodes._answer("选一个", intent="clarify", clarify_items=[{"question": "q"}])
    without = nodes._answer("随便说说", intent="chat")

    assert with_items["clarify_items"] == [{"question": "q"}]
    assert "clarify_items" not in without


async def test_respond_puts_items_into_response_metadata():
    update = await nodes.respond(
        {
            "final_response": "请在下面选一项。",
            "intent": "clarify",
            "clarify_items": [
                {
                    "question": "用在哪？",
                    "field": None,
                    "options": [{"label": "工单", "value": "ticket"}],
                }
            ],
        }
    )

    assert update["response_metadata"]["clarify"]["items"][0]["question"] == "用在哪？"


async def test_respond_merges_with_existing_metadata():
    """先查过工具再反问时，那条 tool 元信息不能被追问项挤掉。"""
    update = await nodes.respond(
        {
            "final_response": "请确认站点。",
            "intent": "clarify",
            "response_metadata": {"tool_name": "prv_query_pressure", "success": True},
            "clarify_items": [{"question": "哪个站？", "options": []}],
        }
    )

    meta = update["response_metadata"]
    assert meta["tool_name"] == "prv_query_pressure"
    assert meta["clarify"]["items"][0]["question"] == "哪个站？"


async def test_respond_ignores_items_when_intent_is_not_clarify():
    """模型在 chat 里顺手给了 clarify_items 时不能当追问卡片发出去。"""
    update = await nodes.respond(
        {
            "final_response": "已按 station-001 处理。",
            "intent": "chat",
            "clarify_items": [{"question": "q", "options": []}],
        }
    )

    assert "response_metadata" not in update


async def test_begin_turn_clears_previous_clarify_items():
    """上一轮的追问项不能留在状态里：本轮若同样反问却只写了一句文字，
    界面会继续显示上一轮那张已经答过的卡片。"""
    reset = await nodes.begin_turn(
        {
            "intent": "clarify",
            "clarify_items": [{"question": "上一轮的问题", "options": []}],
            "tool_observations": [],
            "tool_steps": 0,
        }
    )

    assert reset["clarify_items"] == []


# ---- 槽位兜底：模型把 action 标成 read_tool、但又给了分项追问 ----
#
# 这条路径常被忽略：字段是 schema 校验发现缺的，不是模型主动追问的。
# 模型其实已经把选项想好了，只是归类成了「要调工具」，丢掉就白问一遍。

from app.agents.schemas import ActionPlan  # noqa: E402


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
        self.input_schema = {
            "type": "object",
            "properties": {"station_id": {"type": "string"}},
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


def _install(monkeypatch, plan: ActionPlan) -> None:
    monkeypatch.setattr(nodes, "get_tool_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(nodes, "build_model_tool_catalog", lambda **_kwargs: [])

    async def _plan(**_kwargs) -> ActionPlan:
        return plan

    monkeypatch.setattr(nodes, "create_action_plan", _plan)


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


async def test_missing_slot_keeps_model_options(monkeypatch):
    _install(
        monkeypatch,
        ActionPlan(
            action="read_tool",
            tool_name="get_station_snapshot",
            arguments={},
            reason="还缺站点。",
            missing_fields=["station_id"],
            clarify_items=[
                {
                    "question": "查哪个调压站？",
                    "field": "station_id",
                    "options": [{"label": "城东站", "value": "station-001"}],
                }
            ],
        ),
    )

    result = await nodes.plan_request(_state())

    assert result["intent"] == "clarify"
    assert result["missing_fields"] == ["station_id"]
    assert result["clarify_items"][0]["options"][0]["label"] == "城东站"


async def test_missing_slot_drops_field_outside_missing_list(monkeypatch):
    """模型标了一个这轮根本不缺的字段：field 清空，其余照常收下。"""
    _install(
        monkeypatch,
        ActionPlan(
            action="read_tool",
            tool_name="get_station_snapshot",
            arguments={},
            reason="还缺站点。",
            clarify_items=[
                {
                    "question": "查哪个调压站？",
                    "field": "severity",
                    "options": [{"label": "城东站", "value": "station-001"}],
                }
            ],
        ),
    )

    result = await nodes.plan_request(_state())

    assert result["clarify_items"][0]["field"] is None
    assert result["clarify_items"][0]["options"]
