# tests/integration/test_authorization.py
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.rbac import StationGrant, User

TEST_STATION = "station-001"
TEST_SUBJECT = "test-user"
TEST_TENANT = "default"


async def test_station_without_grant_is_denied(client, planner):
    from app.agents.schemas import ActionPlan

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(StationGrant).where(StationGrant.station_id == "station-999")
            )

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_station_snapshot",
                arguments={"station_id": "station-999"},
                reason="查询未授权场站。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-grant", "message": "看看 station-999 的压力"},
    )

    body = response.json()
    assert body["status"] == "error"
    assert body["error_category"] == "tool_rejected"
    assert "资源不可访问" in body["answer"]


async def test_station_with_grant_returns_mock_marked_data(client, planner):
    from app.agents.schemas import ActionPlan

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_station_snapshot",
                arguments={"station_id": "station-001"},
                reason="查询已授权场站。",
            ),
            # 工具执行完回到规划器，模型基于结果作答——这是推理回路的第二轮。
            ActionPlan(
                action="chat",
                response="当前未配置场站平台地址，返回的是 mock 演示数据。",
            ),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-grant-ok", "message": "看看 station-001 的压力"},
    )

    body = response.json()
    assert body["status"] == "completed"
    assert "mock" in body["answer"]


@pytest.mark.parametrize("permission", ["knowledge.confidential"])
async def test_confidential_scope_absent_from_dev_role(client, permission):
    response = await client.get("/api/v1/knowledge/status")

    assert response.status_code == 200
    assert permission not in response.json()["allowed_scopes"]


async def test_write_tool_on_unauthorized_station_creates_no_approval(client, planner):
    """未授权站点必须在生成审批单之前被拒绝，不能留下孤儿审批记录。"""
    from app.agents.schemas import ActionPlan
    from app.models.approval import ApprovalRequest

    thread_id = "thread-grant-write-denied"

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments={
                    "station_id": "station-999",
                    "title": "越权工单",
                    "description": "不应被创建",
                },
                reason="向未授权场站写数据。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "给 station-999 建工单"},
    )

    body = response.json()
    assert body["status"] == "error"
    assert body["error_category"] == "not_authorized"
    assert body["approval"] is None

    async with async_session_factory() as session:
        rows = list(
            await session.scalars(
                select(ApprovalRequest).where(ApprovalRequest.thread_id == thread_id)
            )
        )
    assert rows == [], "未授权请求不应产生审批记录"


async def test_revoked_grant_fails_deterministically_not_unknown(client, planner):
    """审批期间授权被撤销：结果应是确定失败，而不是 EXECUTION_UNKNOWN。"""
    from app.agents.schemas import ActionPlan

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="create_maintenance_ticket",
                arguments={
                    "station_id": TEST_STATION,
                    "title": "授权撤销测试",
                    "description": "确认前撤销授权",
                },
                reason="测试审批期间授权撤销。",
            )
        ]
    )

    created = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-grant-revoked", "message": "给 station-001 建工单"},
    )

    body = created.json()
    assert body["status"] == "awaiting_approval"
    approval_id = body["approval"]["approval_id"]

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(StationGrant).where(StationGrant.station_id == TEST_STATION)
            )

    try:
        confirmed = await client.post(
            f"/api/v1/approvals/{approval_id}/confirm",
            json={},
        )
        result = confirmed.json()

        assert result["status"] == "FAILED"
        assert result["error_code"] == "NOT_AUTHORIZED"
        assert result["success"] is False
    finally:
        # 恢复授权，避免影响后续用例。
        async with async_session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.external_subject == TEST_SUBJECT)
                )
                exists = await session.scalar(
                    select(StationGrant).where(
                        StationGrant.tenant_id == TEST_TENANT,
                        StationGrant.user_id == user.id,
                        StationGrant.station_id == TEST_STATION,
                    )
                )
                if exists is None:
                    session.add(
                        StationGrant(
                            tenant_id=TEST_TENANT,
                            user_id=user.id,
                            station_id=TEST_STATION,
                            can_read=True,
                            can_write=True,
                        )
                    )
