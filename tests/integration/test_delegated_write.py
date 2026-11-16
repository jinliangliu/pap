# tests/integration/test_delegated_write.py
"""委派写操作（P2）：A 把一件要落笔的事交给 B，审批单由 B 开、人点、B 执行。

这组用例守的是三件在实现里最容易走偏的事：

1. **本图不跟着中断**。子调用停在审批上时，父图既没有中断也没有失败——
   它拿到的是「对方在等确认」。写错成「父图也中断」，恢复时整个委派会被重放一遍；
2. **结论回到发起方的会话**。子线程是一次性的，结论落在那里等于没有结论；
3. **批准期间上游收窄，这张单子作废**。只复核 B 自己的实现能通过前两条，
   却会把「一次已经不该发生的委派」执行掉。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.services import agent_service
from app.tenancy.context import tenant_scope

TENANT = "default"

WRITE_ARGS = {
    "station_id": "station-001",
    "title": "3 号泵压力异常",
    "description": "出口压力低于阈值，需要现场检查。",
}


@pytest.fixture(autouse=True)
async def _clean_agents(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


async def _seed_agent(**kwargs) -> str:
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                session,
                tenant_id=TENANT,
                name=kwargs.pop("name", "ops"),
                display_name=kwargs.pop("display_name", "运行值班"),
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                **kwargs,
            )
            await session.commit()
            return str(row.id)


async def _seed_delegation_pair() -> tuple[str, str]:
    """客服（可写，能建工单）+ 运行值班（可写，只能建工单）。

    客服必须也能建工单：子集校验要求 B 的显式清单是 A 的子集，
    否则委派在第一步就被拒，测不到写操作这条路。
    """
    cs_id = await _seed_agent(
        name="cs",
        display_name="客服",
        allowed_tools=["delegate_to_agent", "create_maintenance_ticket"],
        can_write=True,
    )
    ops_id = await _seed_agent(
        name="ops",
        display_name="运行值班",
        allowed_tools=["create_maintenance_ticket"],
        can_write=True,
    )
    return cs_id, ops_id


async def _delegate_a_write(client, planner, thread_id: str = "thread-delegated-write"):
    """发起一次「把建工单交给运行值班」的对话，返回响应的 JSON。"""
    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="delegate_to_agent",
                arguments={"target": "ops", "task": "给 3 号泵建一张维护工单"},
                reason="建工单属于运行值班的职责。",
            ),
            # 子调用里运行值班决定建工单 —— 它会停在审批上。
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments=WRITE_ARGS,
                reason="用户要求建一张维护工单。",
            ),
            # 控制权回到客服：它只能说「已转交、等确认」，不能替对方宣布完成。
            ActionPlan(
                action="chat",
                response="已经交给运行值班了，它需要你确认后才会真正建单。",
            ),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "给 3 号泵建个维护工单", "agent": "cs"},
    )
    assert response.status_code == 200
    return response.json()


async def _approval(approval_id: str) -> ApprovalRequest:
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(ApprovalRequest, uuid.UUID(approval_id))
            assert row is not None
            session.expunge(row)
            return row


async def _narrow_cs_without_ticket_tool() -> None:
    """把客服收窄到不再包含建工单的能力（模拟「批准期间有人改了配置」）。"""
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="cs")
            await agent_service.update_agent(
                session,
                agent=row,
                allowed_tools=["delegate_to_agent"],
                actor_user_id="admin-1",
            )
            await session.commit()


async def test_delegated_write_waits_for_a_human_and_then_runs(
    client, planner, seeded_identity, session
):
    """整条闭环：转交 → 人点确认 → B 执行 → 结论回到发起方的会话。"""
    await _seed_delegation_pair()

    body = await _delegate_a_write(client, planner)

    # 本图没有中断（它跑完了），但这一轮同样在等人确认。
    assert body["status"] == "awaiting_approval"
    approval_id = body["approval"]["approval_id"]
    assert approval_id
    # 用户在确认之前必须先被告知「还没有写」。模型的转述不算，
    # 服务端自己追加的那句才算——它不会被措辞漂移掉。
    assert "还没" in body["answer"] or "尚未" in body["answer"]

    approval = await _approval(approval_id)
    assert approval.status is ApprovalStatus.PENDING
    # 审批单上要能看出「谁把它叫来的」：否则审批人无从判断这是不是越权链的末端。
    assert approval.delegated_from == "cs"
    assert approval.delegation_depth == 1
    assert approval.parent_thread_id, "缺少发起方会话线程，结论会落到子线程里"
    assert [hop["name"] for hop in approval.delegation_chain] == ["cs"]

    confirm = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirm.status_code == 200
    confirmed = confirm.json()
    assert confirmed["status"] in {"EXECUTED", "VERIFIED"}
    assert confirmed["success"] is True

    # 结论落在**发起方**的会话里，并且点名是谁做的。
    listing = (await client.get("/api/v1/conversations?limit=50")).json()
    target = next(
        item for item in listing["items"] if item["thread_id"] == "thread-delegated-write"
    )
    detail = (await client.get(f"/api/v1/conversations/{target['id']}")).json()
    messages = detail["messages"]

    # 三条：提问、等确认、执行结论。少一条都说明结论没回到这里
    # （落到子线程上时，这条会话只会有前两条，而最后一条仍然是「等确认」）。
    assert len(messages) == 3, [m["content"] for m in messages]
    last = messages[-1]
    assert last["role"] == "assistant"
    assert last["metadata"]["approval_action"] == "confirm"
    assert last["metadata"]["approval_id"] == approval_id
    assert "运行值班" in last["content"]
    # 最后一条是执行结论，不是那句「请确认」。
    assert "需要你确认" not in last["content"]


async def test_narrowing_the_caller_voids_the_pending_approval(
    client, planner, seeded_identity
):
    """批准期间把客服收窄到不再包含这项能力 → 单子作废，外部系统一次都没被写。

    这是 P2 的验收条件。只复核 B 的实现会漏掉这一类：B 自己一点没变，
    变的是它的上游，而那张单子已经是「一次不该发生的委派」的产物。
    """
    await _seed_delegation_pair()

    body = await _delegate_a_write(client, planner, thread_id="thread-delegated-narrow")
    approval_id = body["approval"]["approval_id"]

    await _narrow_cs_without_ticket_tool()

    confirm = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirm.status_code == 200
    result = confirm.json()

    assert result["status"] == "FAILED"
    assert result["success"] is False
    # 理由必须指向「上游变了」，而不是笼统的「执行失败」——
    # 后者会让人去查一个没有任何问题的 B。
    assert result["error_code"] == "DELEGATION_REVOKED"
    # 同一段话里不能既说「已完成」又说「未执行」。实机上踩过一次：
    # 前缀跟着「点了确认」走而不是跟着执行结果走，读者只会记住前一句。
    assert "已完成" not in result["message"]
    assert "没有执行成功" in result["message"]

    approval = await _approval(approval_id)
    assert approval.status is ApprovalStatus.FAILED
    # 没有落过「已提交」的证据：请求根本没发往外部系统。
    assert approval.executed_at is None


async def test_approval_preview_names_the_delegating_agent(
    client, planner, seeded_identity
):
    """审批卡上要写清「在谁的委派下」，而不只是「ops 要建工单」。"""
    await _seed_delegation_pair()

    body = await _delegate_a_write(client, planner, thread_id="thread-delegated-preview")
    approval_id = body["approval"]["approval_id"]

    preview = (await client.get(f"/api/v1/approvals/{approval_id}")).json()
    assert preview["delegated_from"] == "cs"
    assert preview["delegation_depth"] == 1
    assert preview["preview"]["delegation"] == {"from": "cs", "depth": 1}


async def test_non_delegated_approval_has_no_delegation_fields(
    client, planner, seeded_identity
):
    """反向：自己发起写操作时，不能凭空多出一条委派链。

    没有这一条，`_review_delegation` 写错成「总是复核」时所有委派用例仍然绿，
    而普通写操作会开始报一堆莫名其妙的拒绝。
    """
    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments=WRITE_ARGS,
                reason="创建维护工单。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-plain-write", "message": "建个工单"},
    )
    approval_id = response.json()["approval"]["approval_id"]

    preview = (await client.get(f"/api/v1/approvals/{approval_id}")).json()
    assert preview["delegated_from"] is None
    assert preview["delegation_depth"] == 0

    approval = await _approval(approval_id)
    assert approval.delegation_chain is None
    assert approval.parent_thread_id is None

    confirm = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirm.json()["success"] is True


async def test_cancel_puts_the_child_thread_away(client, planner, seeded_identity):
    """取消也要收尾：子线程的检查点不能留在库里等着慢慢攒。"""
    from app.db.checkpoint import get_checkpointer

    await _seed_delegation_pair()

    body = await _delegate_a_write(client, planner, thread_id="thread-delegated-cancel")
    approval_id = body["approval"]["approval_id"]

    cancel = await client.post(f"/api/v1/approvals/{approval_id}/cancel")
    assert cancel.status_code == 200

    storage = getattr(get_checkpointer(), "storage", {}) or {}
    # 只看这条用例自己的子线程：上一条用例留下的那张单子还停在「等人确认」，
    # 它本来就不该被清理（清理发生在确认/取消/过期这三个终态上）。
    leftovers = [
        key for key in storage if "thread-delegated-cancel:d1:ops:" in str(key)
    ]
    assert leftovers == [], f"取消后子线程没有被清理：{leftovers}"


async def test_agents_are_untouched_by_a_rejected_delegation(
    client, planner, seeded_identity, session
):
    """委派写操作不改 Agent 配置：边界只在执行时判定，不会被「批准」固化。"""
    cs_id, ops_id = await _seed_delegation_pair()

    await _delegate_a_write(client, planner, thread_id="thread-delegated-noop")

    rows = (await session.execute(select(Agent))).scalars().all()
    by_id = {str(row.id): row for row in rows}
    assert by_id[cs_id].can_write is True
    assert by_id[ops_id].can_write is True
