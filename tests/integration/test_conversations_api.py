# tests/integration/test_conversations_api.py
"""对话台会话历史接口。

边界比功能重要：只能看到自己的会话，跨用户 / 跨租户一律 404，
删除必须连消息一起删。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.context import ConversationSummary
from app.models.conversation import Message

pytestmark = pytest.mark.anyio

BASE = "/api/v1/conversations"


async def _send(client, thread: str, text: str = "你好") -> None:
    response = await client.post(
        "/api/v1/chat", json={"thread_id": thread, "message": text}
    )
    assert response.status_code == 200


async def test_list_shape_and_limit(client, seeded_identity):
    """不假设库是空的：全量跑时其它用例会先写入会话。"""
    payload = (await client.get(BASE)).json()
    assert isinstance(payload["items"], list)
    assert isinstance(payload["total"], int)
    assert payload["total"] >= len(payload["items"])

    limited = (await client.get(BASE, params={"limit": 1})).json()
    assert len(limited["items"]) <= 1


async def test_chat_then_list_and_detail(client, seeded_identity, planner):
    planner([ActionPlan(action="chat", response="收到，这里是回答。")])

    await _send(client, "conv-e2e-1", "第一条消息")

    listing = (await client.get(BASE)).json()
    assert listing["total"] >= 1

    item = [c for c in listing["items"] if c["thread_id"] == "conv-e2e-1"][0]
    assert item["title"] == "第一条消息"
    assert item["message_count"] == 2
    # 内部 ID 形如 tenant:user:client，不能出现在响应里。
    assert ":" not in item["thread_id"]

    detail = (await client.get(f"{BASE}/{item['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "第一条消息"
    assert detail["messages"][1]["content"] == "收到，这里是回答。"


async def test_continue_existing_thread_reuses_conversation(
    client, seeded_identity, planner
):
    planner(
        [
            ActionPlan(action="chat", response="第一次回答。"),
            ActionPlan(action="chat", response="第二次回答。"),
        ]
    )

    await _send(client, "conv-e2e-2", "第一轮")
    await _send(client, "conv-e2e-2", "第二轮")

    listing = (await client.get(BASE)).json()
    matched = [c for c in listing["items"] if c["thread_id"] == "conv-e2e-2"]
    assert len(matched) == 1
    assert matched[0]["message_count"] == 4


async def test_rename_conversation(client, seeded_identity, planner):
    planner([ActionPlan(action="chat", response="好。")])
    await _send(client, "conv-e2e-3", "原标题")

    item = [
        c for c in (await client.get(BASE)).json()["items"]
        if c["thread_id"] == "conv-e2e-3"
    ][0]

    renamed = await client.patch(f"{BASE}/{item['id']}", json={"title": "新标题"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新标题"

    detail = (await client.get(f"{BASE}/{item['id']}")).json()
    assert detail["conversation"]["title"] == "新标题"
    # 改名不能碰消息内容。
    assert detail["messages"][0]["content"] == "原标题"


async def test_delete_removes_messages(client, seeded_identity, planner):
    planner([ActionPlan(action="chat", response="好。")])
    await _send(client, "conv-e2e-4", "待删除")

    item = [
        c for c in (await client.get(BASE)).json()["items"]
        if c["thread_id"] == "conv-e2e-4"
    ][0]

    deleted = await client.delete(f"{BASE}/{item['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    assert (await client.get(f"{BASE}/{item['id']}")).status_code == 404

    async with async_session_factory() as session:
        left = (
            await session.scalars(
                select(Message).where(Message.conversation_id == uuid.UUID(item["id"]))
            )
        ).all()
    assert left == []


async def test_other_user_cannot_read(client, other_client, seeded_identity, planner):
    planner([ActionPlan(action="chat", response="好。")])
    await _send(client, "conv-e2e-5", "私有会话")

    item = [
        c for c in (await client.get(BASE)).json()["items"]
        if c["thread_id"] == "conv-e2e-5"
    ][0]

    assert (await other_client.get(f"{BASE}/{item['id']}")).status_code == 404
    assert (await other_client.delete(f"{BASE}/{item['id']}")).status_code == 404
    assert (
        await other_client.patch(f"{BASE}/{item['id']}", json={"title": "改别人的"})
    ).status_code == 404

    # 别人列表里也不该出现。
    others = (await other_client.get(BASE)).json()["items"]
    assert all(c["id"] != item["id"] for c in others)


# ---------------- channel：构建助手的会话 ----------------
#
# 面板要能在刷新后把对话读回来，所以会话得能按入口筛。
# 这两条守的是「筛得准」与「读得到」：前者靠 channel，后者靠
# 「等审批的那句话也落库」——只把提问写进库，刷新后就只剩一句没有下文的问话。


async def _grant(session, permission: str) -> None:
    """给 tester 角色补一个权限。

    与 test_agents.py 里那份同形，各自持有一份：这是「这条用例要不要写权限」
    的局部决定，共用一个全局改权限的 fixture 会让用例之间互相影响。
    """
    from app.models.rbac import Role, RolePermission

    role = await session.scalar(select(Role).where(Role.name == "tester"))
    assert role is not None

    exists = await session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission == permission,
        )
    )
    if exists is None:
        session.add(RolePermission(role_id=role.id, permission=permission))
        await session.commit()


async def _find(client, thread: str) -> dict:
    listing = (await client.get(BASE, params={"limit": 200})).json()
    matched = [c for c in listing["items"] if c["thread_id"] == thread]
    assert matched, f"列表里找不到 {thread}"
    return matched[0]


async def test_channel_filter_separates_studio_from_api(client, seeded_identity, planner):
    """构建助手的会话不混进对话台，对话台的也不混进助手的面板。"""
    planner(
        [
            ActionPlan(action="chat", response="对话台回答。"),
            ActionPlan(action="chat", response="助手回答。"),
        ]
    )

    await client.get("/api/v1/agents")  # 惰性创建内置助手
    await _send(client, "conv-ch-api", "普通会话")

    studio = await client.post(
        "/api/v1/chat",
        json={"thread_id": "conv-ch-studio", "message": "助手会话", "agent": "studio"},
    )
    assert studio.status_code == 200
    assert studio.json()["status"] == "completed"

    # 不假设库是空的：同文件里其它用例也会留下 api 会话。
    api_only = (await client.get(BASE, params={"channel": "api", "limit": 200})).json()
    api_threads = {c["thread_id"] for c in api_only["items"]}
    assert "conv-ch-api" in api_threads
    assert "conv-ch-studio" not in api_threads
    assert all(c["channel"] == "api" for c in api_only["items"])

    studio_only = (await client.get(BASE, params={"channel": "studio", "limit": 200})).json()
    studio_threads = {c["thread_id"] for c in studio_only["items"]}
    assert "conv-ch-studio" in studio_threads
    assert "conv-ch-api" not in studio_threads
    assert all(c["channel"] == "studio" for c in studio_only["items"])
    # 总数要跟条件一致：两处各写一遍条件时，出现过「列表 1 条、总数 1 条」之外的
    # 情况就是这里会红。
    assert studio_only["total"] == len(studio_only["items"])

    everything = (await client.get(BASE, params={"limit": 200})).json()
    assert {"conv-ch-api", "conv-ch-studio"} <= {c["thread_id"] for c in everything["items"]}


async def test_pending_approval_message_is_persisted(client, session, seeded_identity, planner):
    """等审批的那一句要落库，且带着审批号——面板靠它把确认卡拼回来。"""
    await _grant(session, "agent:write")
    await client.get("/api/v1/agents")

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="studio_agent_save",
                arguments={
                    "mode": "create",
                    "name": "cs",
                    "display_name": "客服",
                    "can_write": True,
                },
                reason="用户要求新建客服智能体。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "conv-ch-pending",
            "message": "建一个客服 Agent",
            "agent": "studio",
        },
    )
    body = response.json()
    assert body["status"] == "awaiting_approval"
    approval_id = body["approval"]["approval_id"]

    item = await _find(client, "conv-ch-pending")
    assert item["channel"] == "studio"

    detail = (await client.get(f"{BASE}/{item['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    pending = detail["messages"][1]
    assert pending["content"] == body["answer"]
    assert pending["metadata"]["approval_id"] == approval_id

    # 确认之后结论也要进历史：否则刷新后人以为没执行，会再发一遍。
    confirmed = await client.post(
        f"/api/v1/approvals/{approval_id}/confirm", json={}
    )
    assert confirmed.status_code == 200

    after = (await client.get(f"{BASE}/{item['id']}")).json()
    assert [m["role"] for m in after["messages"]] == ["user", "assistant", "assistant"]
    assert after["messages"][2]["content"]
    assert after["messages"][2]["metadata"]["approval_action"] == "confirm"


async def test_detail_returns_context_summaries(
    client, session, seeded_identity, planner
):
    """压缩摘要随会话详情一起返回，供前端在边界插分隔条。

    没压过时必须是空数组而不是缺字段：前端据「是否有 summaries」决定画不画，
    字段缺失会让整页渲染中断。
    """
    planner([ActionPlan(action="chat", response="回答。")])
    await _send(client, "conv-ctx-detail", "第一轮")

    item = await _find(client, "conv-ctx-detail")
    before = (await client.get(f"{BASE}/{item['id']}")).json()
    assert before["summaries"] == []

    first_user = before["messages"][0]
    session.add(
        ConversationSummary(
            tenant_id=seeded_identity["tenant_id"],
            conversation_id=uuid.UUID(item["id"]),
            through_message_id=uuid.UUID(first_user["id"]),
            through_created_at=datetime.now(UTC),
            turns_covered=1,
            messages_covered=2,
            tokens_before=800,
            tokens_after=120,
            summary="【当前目标】验证水位",
            summary_chars=9,
            model="fake-summary",
        )
    )
    await session.commit()

    detail = (await client.get(f"{BASE}/{item['id']}")).json()
    assert len(detail["summaries"]) == 1

    only = detail["summaries"][0]
    assert only["through_message_id"] == first_user["id"]
    assert only["turns_covered"] == 1
    assert only["summary"].startswith("【当前目标】")
    assert only["tokens_before"] == 800
    assert only["tokens_after"] == 120


async def test_workflow_card_and_conclusion_both_carry_the_graph(
    client, session, seeded_identity, planner
):
    """图要跟着审批单和结论各留一份，两条路都能单独还原。

    线上出现过的形状：卡片只有一行「新建或修改编排流程」+ 一份折叠的 definition，
    用户扫一眼就点了确认——而那张图有分叉，文字里只体现为一个分号。图随
    `preview` 落进审批单（刷新后取得到），结论那条消息另存一份 `metadata.graph`
    （确认完卡片变成「已处理」，人这时候核对的正是结论那段）。
    """
    from app.models.agent import Agent, AgentStatus

    session.add(
        Agent(
            tenant_id="default",
            name="ops",
            display_name="运行值班",
            status=AgentStatus.ACTIVE,
        )
    )
    await session.commit()

    await _grant(session, "agent:write")
    # 构建助手是首次访问惰性创建的：不先读一次列表，chat 会因为「智能体不存在」被拒。
    await client.get("/api/v1/agents")

    definition = {
        "entry": "check",
        "nodes": [
            {
                "id": "check",
                "agent": "ops",
                "task": "查告警",
                "emit": {"field": "level", "values": ["严重", "无"]},
                "next": [
                    {"when": {"key": "level", "op": "eq", "value": "严重"}, "to": "act"},
                    {"when": None, "to": "report"},
                ],
            },
            {"id": "act", "agent": "ops", "task": "处置", "next": [{"when": None, "to": "report"}]},
            {"id": "report", "kind": "compose", "task": "日报", "next": []},
        ],
    }

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="studio_workflow_save",
                arguments={
                    "mode": "create",
                    "name": "graph-check",
                    "display_name": "分级处置",
                    "definition": definition,
                },
                reason="用户描述了有先后顺序与分流的处置过程。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "conv-graph-card",
            "message": "建一个分级处置流程",
            "agent": "studio",
        },
    )
    body = response.json()
    assert body.get("status") == "awaiting_approval", body

    graph = body["approval"]["preview"]["graph"]
    assert [node["id"] for node in graph["nodes"]] == ["check", "act", "report"]
    # 兜底边必须在：缺了它，图上看着像「没有分流」。
    assert any(edge["when"] is None and edge["to"] == "report" for edge in graph["edges"])

    confirmed = await client.post(
        f"/api/v1/approvals/{body['approval']['approval_id']}/confirm", json={}
    )
    assert confirmed.status_code == 200

    item = await _find(client, "conv-graph-card")
    detail = (await client.get(f"{BASE}/{item['id']}")).json()
    last = detail["messages"][-1]
    assert last["metadata"]["approval_action"] == "confirm"
    assert [node["id"] for node in last["metadata"]["graph"]["nodes"]] == [
        "check",
        "act",
        "report",
    ]

    # 收尾：这条用例真的建了一张流程和一个智能体，别的用例按条数断言时会撞上
    # （观测拓扑那边就有 `workflow_count == 1`）。
    from app.models.workflow import Workflow

    for row in (await session.scalars(select(Workflow).where(Workflow.name == "graph-check"))):
        await session.delete(row)
    for row in (await session.scalars(select(Agent).where(Agent.name == "ops"))):
        await session.delete(row)
    await session.commit()
