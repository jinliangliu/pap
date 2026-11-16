# tests/integration/test_approval_api.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents.schemas import ActionPlan
from app.models.approval import ApprovalRequest
from app.models.audit import AuditEventType, AuditLog

WRITE_ARGS = {
    "station_id": "station-001",
    "title": "压力异常",
    "description": "出口压力低于阈值。",
}


async def _create_pending_approval(client, planner, thread_id: str) -> str:
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
        json={"thread_id": thread_id, "message": "创建工单"},
    )
    assert response.json()["status"] == "awaiting_approval"

    return response.json()["approval"]["approval_id"]


async def test_unknown_approval_returns_404(client):
    response = await client.get(f"/api/v1/approvals/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_invalid_approval_id_returns_422(client):
    response = await client.get("/api/v1/approvals/not-a-uuid")

    assert response.status_code == 422


async def test_mismatched_parameters_are_rejected(client, planner):
    approval_id = await _create_pending_approval(client, planner, "thread-mismatch")

    response = await client.post(
        f"/api/v1/approvals/{approval_id}/confirm",
        json={"parameters": {"title": "被篡改的标题"}},
    )

    assert response.status_code == 422
    assert "不一致" in response.json()["detail"]


async def test_mismatched_parameters_leave_audit_trail(client, planner, session):
    """参数篡改要被拒绝，并且留下可追溯的记录（只记键名，不记值）。"""
    approval_id = await _create_pending_approval(client, planner, "thread-mismatch-audit")

    response = await client.post(
        f"/api/v1/approvals/{approval_id}/confirm",
        json={"parameters": {"title": "机密内容不应入审计"}},
    )

    assert response.status_code == 422

    rows = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.event_type == AuditEventType.APPROVAL_PARAMETER_MISMATCH,
                AuditLog.resource_id == approval_id,
            )
        )
    ).all()

    assert len(rows) == 1
    event = rows[0]
    assert event.success is False
    assert event.metadata_json.get("tool_name") == "create_maintenance_ticket"
    assert event.metadata_json.get("submitted_keys") == ["title"]
    # 值不能进审计，否则审计表成了敏感数据的旁路存储。
    assert "机密内容" not in str(event.metadata_json)


async def test_cancel_sets_rejected_status(client, planner):
    approval_id = await _create_pending_approval(client, planner, "thread-cancel")

    response = await client.post(f"/api/v1/approvals/{approval_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["verified"] is False

    detail = await client.get(f"/api/v1/approvals/{approval_id}")
    assert detail.json()["status"] == "REJECTED"


async def test_confirm_after_cancel_returns_409(client, planner):
    """已取消的审批再确认是状态冲突，不能回 200 让调用方误判为提交成功。"""
    approval_id = await _create_pending_approval(client, planner, "thread-cancel-409")

    await client.post(f"/api/v1/approvals/{approval_id}/cancel")

    response = await client.post(f"/api/v1/approvals/{approval_id}/confirm")

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["success"] is False
    assert "不能执行" in body["message"]


async def test_expiry_detected_while_executing_returns_409(client, planner, session):
    """预检时还是 PENDING，执行期才被判过期：状态码同样必须是 409。

    原先两条路不一致——清理任务先跑到就 409，没跑到就 200 带 `status=EXPIRED`。
    同一个事实（什么都没执行）因为时序不同给出两个状态码，客户端照文档里
    「409 = 已过期」写的那套判断，会漏掉一半情况。

    同一条分支还兜着「预检后单子被别处取消 / 抢先进入执行中」——它们是同一个
    集合（`NOT_EXECUTABLE`），这里只验证能在测试里稳定复现的过期这一种。
    """
    approval_id = await _create_pending_approval(client, planner, "thread-expire-exec")

    approval = await session.get(ApprovalRequest, uuid.UUID(approval_id))
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    response = await client.post(f"/api/v1/approvals/{approval_id}/confirm")

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "EXPIRED"
    assert body["success"] is False
    # 那句话必须原样回来：卡上要写「为什么没执行」，只给一个状态码等于让人去猜。
    assert "过期" in body["message"]

    session.expire_all()
    stored = await session.get(ApprovalRequest, uuid.UUID(approval_id))
    assert stored.status.value == "EXPIRED"
    assert stored.executed_at is None


async def test_repeated_cancel_returns_409(client, planner):
    """取消也走同一套状态码约定，客户端不必为两个端点写两套判断。"""
    approval_id = await _create_pending_approval(client, planner, "thread-cancel-twice")

    first = await client.post(f"/api/v1/approvals/{approval_id}/cancel")
    assert first.status_code == 200

    second = await client.post(f"/api/v1/approvals/{approval_id}/cancel")

    assert second.status_code == 409
    assert second.json()["success"] is False
    assert "无需重复取消" in second.json()["message"]


async def test_cancel_after_completion_returns_409(client, planner):
    """
    已经执行完成的审批不能被「取消」成 200。

    那会让用户以为自己成功阻止了一次已经发生的写入，
    是这类接口里最不能含糊的一种误解。
    """
    approval_id = await _create_pending_approval(client, planner, "thread-cancel-done")

    confirmed = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirmed.status_code == 200

    response = await client.post(f"/api/v1/approvals/{approval_id}/cancel")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert "已完成" in body["message"]


# ---------------- 批量取：刷新会话时还原审批卡 ----------------
#
# 会话里只存审批号，卡上的参数与当前状态都在单子上。刷新后要按号取回来，
# 而且一次取一批——一条长会话可能攒了几十张卡。


async def _batch(client, ids: list[str]):
    return await client.get("/api/v1/approvals", params={"ids": ",".join(ids)})


async def test_batch_returns_the_requested_approvals(client, planner):
    first = await _create_pending_approval(client, planner, "thread-batch-a")
    second = await _create_pending_approval(client, planner, "thread-batch-b")

    response = await _batch(client, [first, second])

    assert response.status_code == 200
    got = {item["approval_id"] for item in response.json()["items"]}
    assert got == {first, second}


async def test_batch_hides_other_tenants_approvals(client, planner, other_client):
    """按 (租户, 用户) 收口：知道审批号也读不到别人的单子。"""
    approval_id = await _create_pending_approval(client, planner, "thread-batch-scope")

    response = await _batch(other_client, [approval_id])

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_batch_skips_unknown_and_malformed_ids(client, planner):
    """失效号不能让整段会话读不出来——少的那张由前端显示成「已失效」。"""
    approval_id = await _create_pending_approval(client, planner, "thread-batch-mixed")

    response = await _batch(client, [approval_id, str(uuid.uuid4()), "not-a-uuid", ""])

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["approval_id"] for item in items] == [approval_id]


async def test_batch_without_ids_returns_empty(client):
    response = await client.get("/api/v1/approvals")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_pending_approval_is_actionable(client, planner):
    approval_id = await _create_pending_approval(client, planner, "thread-batch-live")

    item = (await _batch(client, [approval_id])).json()["items"][0]

    assert item["status"] == "PENDING"
    assert item["actionable"] is True
    # 卡上要还原出参数，否则刷新后只剩一句「需要确认」，人不知道自己确认的是什么。
    assert item["preview"]["parameters"]["station_id"] == "station-001"
    assert item["preview"]["tool_name"] == "create_maintenance_ticket"


async def test_expired_but_unreaped_approval_is_not_actionable(
    client, planner, session
):
    """状态还写着 PENDING，但过期时间已过——不能给按钮。

    清理任务是定时跑的，这两者之间一定有一个窗口。窗口里继续给「确认执行」，
    点下去只会拿到 409；而且这是**服务端**该判的事：浏览器时钟差几分钟，
    就会把一张还能确认的单子说成已过期。
    """
    approval_id = await _create_pending_approval(client, planner, "thread-batch-stale")

    approval = await session.get(ApprovalRequest, uuid.UUID(approval_id))
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    item = (await _batch(client, [approval_id])).json()["items"][0]

    assert item["status"] == "PENDING"
    assert item["actionable"] is False


async def test_settled_approval_reports_its_outcome(client, planner):
    """处理完的单子要带着结果回来，卡上才写得出「已取消」而不是继续等人。"""
    approval_id = await _create_pending_approval(client, planner, "thread-batch-canceled")

    await client.post(f"/api/v1/approvals/{approval_id}/cancel")

    item = (await _batch(client, [approval_id])).json()["items"][0]

    assert item["status"] == "REJECTED"
    assert item["actionable"] is False


async def test_batch_hides_a_colleagues_approvals(client, planner, session, seeded_identity):
    """同租户的另一个用户，也不该读到这张单子。

    跨租户由 ORM 的自动过滤兜着，这条测的是另一端：审批单按用户归属，
    批量读必须和单条读一样带上 `user_id`——否则同事能看到别人要确认的是什么。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.models.rbac import Role, User, UserRole

    approval_id = await _create_pending_approval(client, planner, "thread-batch-colleague")

    role = await session.scalar(select(Role).where(Role.name == "tester"))
    colleague = User(external_subject="colleague-batch", display_name="同事", enabled=True)
    session.add(colleague)
    await session.flush()
    session.add(
        UserRole(
            tenant_id=seeded_identity["tenant_id"],
            user_id=colleague.id,
            role_id=role.id,
        )
    )
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": "colleague-batch",
            "X-Authenticated-Tenant": seeded_identity["tenant_id"],
        },
    ) as colleague_client:
        response = await _batch(colleague_client, [approval_id])

    assert response.status_code == 200
    assert response.json()["items"] == []
