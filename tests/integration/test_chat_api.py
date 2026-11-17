# tests/integration/test_chat_api.py
from __future__ import annotations

import dataclasses

from app.agents.schemas import ActionPlan
from app.tools.dependencies import get_tool_gateway


async def test_chat_returns_answer(client, planner):
    planner([ActionPlan(action="chat", response="今天是周一。")])

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-chat", "message": "今天周几？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["answer"] == "今天是周一。"
    assert body["run_id"]


async def test_model_not_configured_returns_explicit_error(client):
    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-nomodel", "message": "你好"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error_category"] == "model_not_configured"
    assert "未配置模型" in body["answer"]


async def test_read_tool_path_executes_and_reports_result(client, planner):
    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_platform_time",
                arguments={"timezone": "Asia/Shanghai"},
                reason="查询服务器时间。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-read", "message": "现在服务器几点？"},
    )

    body = response.json()
    assert body["status"] == "completed"
    assert body["metadata"]["tool_name"] == "get_platform_time"
    assert body["metadata"]["success"] is True


async def test_write_tool_requires_approval_and_executes_once(client, planner):
    gateway = get_tool_gateway()
    tool = gateway.get_tool("create_maintenance_ticket")

    calls: list[str | None] = []

    async def counting_handler(context, arguments):  # noqa: ANN001
        calls.append(context.idempotency_key)
        return await tool.handler(context, arguments)

    gateway.register_tool(
        dataclasses.replace(tool, handler=counting_handler),
        replace=True,
    )

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments={
                    "station_id": "station-001",
                    "title": "压力异常",
                    "description": "出口压力低于阈值，需要现场检查。",
                },
                reason="为异常场站创建维护工单。",
            )
        ]
    )

    chat = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-write", "message": "给 station-001 建个维护工单"},
    )

    body = chat.json()
    assert body["status"] == "awaiting_approval"
    approval_id = body["approval"]["approval_id"]
    assert calls == [], "未确认前绝不能执行外部写操作"

    confirmed = await client.post(f"/api/v1/approvals/{approval_id}/confirm", json={})

    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["status"] in {"VERIFIED", "EXECUTED"}
    assert result["verified"] is True
    assert len(calls) == 1

    # 再次确认不得重复执行
    again = await client.post(f"/api/v1/approvals/{approval_id}/confirm", json={})

    assert again.status_code == 200
    assert again.json()["status"] == result["status"]
    assert len(calls) == 1, "同一审批只能执行一次"


async def test_knowledge_path_reports_missing_evidence(client, planner):
    planner([ActionPlan(action="knowledge", query="不存在的主题")])

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-knowledge", "message": "查一下内部规范"},
    )

    body = response.json()
    assert body["status"] == "completed"
    assert "未找到足以支持该结论的资料" in body["answer"]


async def test_bubble_time_matches_stored_message_time(client, planner):
    """气泡上的时间来自服务端落库时刻，与刷新后的历史逐字一致。

    若只靠浏览器时钟，同一句话「刚发出去」和「刷新后」会是两个时间：
    一个来自本机、一个来自库里，客户端时钟偏一点就对不上。
    """
    planner([ActionPlan(action="chat", response="好的。")])

    body = (
        await client.post(
            "/api/v1/chat",
            json={"thread_id": "thread-bubble-time", "message": "记一下时间"},
        )
    ).json()

    asked = body["user_message_at"]
    answered = body["assistant_message_at"]
    assert asked and answered
    # 带时区（UTC），前端才能换算成本地时间显示
    assert asked.endswith("+00:00") or asked.endswith("Z")
    assert answered >= asked

    listing = (await client.get("/api/v1/conversations")).json()["items"]
    conv = next(item for item in listing if item["thread_id"] == "thread-bubble-time")
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}")).json()

    stamps = [
        message["created_at"]
        for message in detail["messages"]
        if message["role"] in ("user", "assistant")
    ]
    assert stamps == [asked, answered]


async def test_completed_turn_schedules_memory_extraction(
    client, planner, monkeypatch
):
    """抽取只挂在「答完了」这一条路径上，参数要带上这一轮的一问一答。"""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.memory_extract.schedule_extraction",
        lambda **kwargs: calls.append(kwargs),
    )
    planner([ActionPlan(action="chat", response="好的 dylan。")])

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-mem", "message": "以后叫我 dylan 就行"},
    )

    assert response.json()["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["user_message"] == "以后叫我 dylan 就行"
    assert calls[0]["answer"] == "好的 dylan。"
    assert calls[0]["thread_id"]


async def test_failed_turn_does_not_schedule_extraction(client, monkeypatch):
    """模型没配好的这一轮没有结论可提炼，不该产生抽取调用。"""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.memory_extract.schedule_extraction",
        lambda **kwargs: calls.append(kwargs),
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-mem-err", "message": "以后叫我 dylan 就行"},
    )

    assert response.json()["status"] == "error"
    assert calls == []
