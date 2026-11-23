# tests/unit/test_studio_build_nudge.py
"""构建助手里「说了一段先后、却只回了一段话」的那一轮，要被纠一次。

五轮线上探针的结论：光写职责说明不管用。同一条消息（`/clear` 之后说「压力高了先
查规程再决定关不关阀」）反复出现四种绕路——

1. 「这条我记下了……要我按这个顺序起草一版，还是直接保存？」（把两件事推回给用户）
2. 「这句是原则，不是一次具体动作，两种做法：查一次现场 / 建成流程，要哪个？」
3. 查完流程库（0 张）就答「当前租户还没有任何编排流程。新建用 …mode=create……」
4. 在回答里手画一张 ```svg 流程图——看着像交了图，库里没有。

说到「由谁做」的那句（「先让现场作业查一次读数，再让通用助手对照规程给判断」）
每次都直接走到了确认卡。差别不在句式长短，在于模型要不要自己决定分工——它倾向
把「分工」当用户的事，于是先问一句。

所以入口这一层加一次纠正：构建助手 + chat + 这句话有先后 → 追加「本轮要求」重规划
一次。只纠一次、只对构建助手，别的智能体说一段先后很可能就是要你照着做。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from app.agents import nodes
from app.agents.prompts import STUDIO_BUILD_NUDGE
from app.agents.schemas import ActionPlan

pytestmark = pytest.mark.anyio


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
        self.input_schema = {
            "type": "object",
            "properties": {"definition": {"type": "object"}},
            "required": ["definition"],
            "additionalProperties": False,
        }


class _FakeGateway:
    async def refresh_tenant_policy(self, _tenant_id: str) -> None:
        return None

    def get_tool(self, name: str) -> _FakeTool:
        return _FakeTool(name)

    def requires_approval(self, _tool: _FakeTool) -> bool:
        return False

    def requires_approval_for(self, *, tool, context) -> bool:
        return False


def _install(monkeypatch, plans: list[ActionPlan]) -> list[dict]:
    """按顺序返回计划的假规划器；返回它收到的每次入参，供断言。"""
    calls: list[dict] = []
    queue = list(plans)

    monkeypatch.setattr(nodes, "get_tool_gateway", lambda: _FakeGateway())
    monkeypatch.setattr(nodes, "build_model_tool_catalog", lambda **_kwargs: [])

    async def _plan(**kwargs) -> ActionPlan:
        calls.append(kwargs)
        return queue[min(len(calls) - 1, len(queue) - 1)]

    monkeypatch.setattr(nodes, "create_action_plan", _plan)
    return calls


def _state(message: str, agent_name: str = "studio"):
    return {
        "tenant_id": "default",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "agent_name": agent_name,
        "messages": [HumanMessage(content=message)],
        "scopes": [],
        "roles": [],
        "tool_observations": [],
        "tool_steps": 0,
    }


CHAT = ActionPlan(action="chat", response="这条处置顺序我记下了，要我起草一版还是直接保存？")
DRAFT = ActionPlan(
    action="read_tool",
    tool_name="studio_workflow_draft",
    arguments={"definition": {"entry": "a", "nodes": []}},
    reason="用户说了一段先后，按最保守的排一版。",
)


@pytest.mark.parametrize(
    "message",
    [
        "压力高了先查规程再决定关不关阀",
        "泄漏报警时先查定位记录，再由现场确认严重程度，最后决定派不派人",
        "班中压力异常，先让现场作业查一次读数，再让通用助手对照规程给判断",
    ],
)
def test_process_description_is_recognised(message: str) -> None:
    assert nodes._looks_like_process(message)


@pytest.mark.parametrize(
    "message",
    [
        "帮我查一下 3 号站现在压力多少",
        "先查一下压力",  # 只有起手，没有接续
        "怎么建一张编排流程？",
        "这张图现在能不能存",
        "",
    ],
)
def test_other_messages_are_left_alone(message: str) -> None:
    assert not nodes._looks_like_process(message)


async def test_chat_is_retried_into_a_draft(monkeypatch) -> None:
    calls = _install(monkeypatch, [CHAT, DRAFT])
    result = await nodes.plan_request(_state("压力高了先查规程再决定关不关阀"))

    assert len(calls) == 2
    # 纠正语必须进 system（agent_block 就是拼在 system 末尾的那段），不是进 payload：
    # 进 payload 会被当成「数据」，模型会当事实引用。
    assert STUDIO_BUILD_NUDGE in calls[1]["agent_block"]
    assert STUDIO_BUILD_NUDGE not in calls[0]["agent_block"]
    assert result["planned_tool_name"] == "studio_workflow_draft"
    assert result["intent"] != "chat"


async def test_retry_happens_once(monkeypatch) -> None:
    """纠完还是 chat：沿用原来的答复，不能无限重规划。"""
    calls = _install(monkeypatch, [CHAT, CHAT])
    result = await nodes.plan_request(_state("压力高了先查规程再决定关不关阀"))

    assert len(calls) == 2
    assert result["intent"] == "chat"
    assert "直接保存" in result["final_response"]


async def test_other_agents_are_not_retried(monkeypatch) -> None:
    """业务助手里说一段先后，可能就是要你照着做一遍——不能把它纠成「去建图」。"""
    calls = _install(monkeypatch, [CHAT, DRAFT])
    result = await nodes.plan_request(
        _state("压力高了先查规程再决定关不关阀", agent_name="field-ops")
    )

    assert len(calls) == 1
    assert result["intent"] == "chat"


async def test_plain_question_is_not_retried(monkeypatch) -> None:
    calls = _install(monkeypatch, [CHAT, DRAFT])
    result = await nodes.plan_request(_state("怎么建一张编排流程？"))

    assert len(calls) == 1
    assert result["intent"] == "chat"
