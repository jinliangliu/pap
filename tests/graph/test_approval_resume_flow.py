# tests/graph/test_approval_resume_flow.py
from __future__ import annotations

from app.agents.graph import init_agent_graph
from app.agents.schemas import ActionPlan
from app.db.checkpoint import get_checkpointer

WRITE_ARGS = {
    "station_id": "station-001",
    "title": "恢复测试",
    "description": "验证中断后可恢复。",
}


async def test_pending_approval_survives_graph_rebuild(client, planner):
    """
    模拟服务重启：重新用同一 checkpointer 编译执行图，
    之前中断的审批仍应可以恢复执行。
    """
    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments=WRITE_ARGS,
                reason="创建工单。",
            )
        ]
    )

    chat = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-restart", "message": "创建工单"},
    )
    approval_id = chat.json()["approval"]["approval_id"]

    # 重建执行图，等价于进程重启后重新装配
    init_agent_graph(get_checkpointer())

    confirmed = await client.post(f"/api/v1/approvals/{approval_id}/confirm", json={})

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] in {"VERIFIED", "EXECUTED"}


async def test_unknown_thread_resume_returns_conflict(client):
    response = await client.post(
        "/api/v1/approvals/00000000-0000-0000-0000-000000000000/confirm",
        json={},
    )

    assert response.status_code == 404
