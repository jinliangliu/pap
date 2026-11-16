# tests/integration/test_multi_tenant.py
"""多租户隔离的端到端验证。

关注点不是「功能能用」，而是「越界一定失败」：

- 请求头里的租户只能申请，不能决定；
- 跨租户读会话/附件一律 404（不是 403，避免泄露「存在但你看不到」）；
- 平台管理通道必须显式、必须留审计；
- 同一个客户端 thread_id 在两个租户里互不相干。
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.main import app
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.rbac import User
from app.tenancy.threads import internal_thread_id
from tests.conftest import (
    OTHER_SUBJECT,
    OTHER_TENANT,
    TEST_SUBJECT,
    TEST_TENANT,
)


def _as(subject: str, tenant_id: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": tenant_id,
        },
    )


async def test_me_reports_tenant_and_roles(client):
    response = await client.get("/api/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == TEST_SUBJECT
    assert body["tenant_id"] == TEST_TENANT
    assert "tester" in body["roles"]
    assert body["is_platform_admin"] is False
    # 只声明自己所属的租户，不泄露平台里还有哪些租户。
    assert body["tenants"] == [TEST_TENANT]


async def test_switching_to_foreign_tenant_is_rejected(seeded_identity):
    """请求头申请一个自己不属于的租户 → 403，而不是悄悄按请求头执行。"""
    async with _as(TEST_SUBJECT, OTHER_TENANT) as http:
        response = await http.get("/api/v1/me")

    assert response.status_code == 403


async def test_unknown_tenant_is_rejected(seeded_identity):
    async with _as(TEST_SUBJECT, "tenant-does-not-exist") as http:
        response = await http.get("/api/v1/me")

    assert response.status_code == 403


async def test_chat_thread_ids_are_namespaced_per_tenant(client, other_client, planner):
    """两个租户使用同一个客户端 thread_id，必须落到两条互不相干的会话。"""
    planner(
        [
            ActionPlan(action="chat", response="租户 A 的回答。"),
            ActionPlan(action="chat", response="租户 B 的回答。"),
        ]
    )

    first = await client.post(
        "/api/v1/chat",
        json={"thread_id": "shared-thread", "message": "同样的线程号"},
    )
    second = await other_client.post(
        "/api/v1/chat",
        json={"thread_id": "shared-thread", "message": "同样的线程号"},
    )

    assert first.status_code == second.status_code == 200
    # 对客户端来说 thread_id 保持一致，命名空间只存在于服务端。
    assert first.json()["thread_id"] == second.json()["thread_id"] == "shared-thread"

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Conversation.tenant_id, Conversation.thread_id).where(
                    Conversation.thread_id.like("%shared-thread")
                )
            )
        ).all()

    prefixes = {tenant for tenant, _ in rows}

    assert prefixes == {TEST_TENANT, OTHER_TENANT}
    assert len(rows) == 2
    assert all(thread_id.endswith(":shared-thread") for _, thread_id in rows)


async def test_internal_thread_id_cannot_be_forged(client, other_client, planner):
    """直接把「别人的内部 thread_id」当客户端 ID 传进来，只会得到自己命名空间下的新会话。"""
    planner(
        [
            ActionPlan(action="chat", response="A 的回答。"),
            ActionPlan(action="chat", response="B 的回答。"),
        ]
    )

    await client.post(
        "/api/v1/chat",
        json={"thread_id": "mine", "message": "先建一条"},
    )

    async with async_session_factory() as session:
        target = await session.scalar(
            select(Conversation.thread_id).where(Conversation.tenant_id == TEST_TENANT)
        )
        other_user_id = await session.scalar(
            select(User.id).where(User.external_subject == OTHER_SUBJECT)
        )

    assert target is not None
    assert other_user_id is not None

    leaked = await other_client.post(
        "/api/v1/chat",
        json={"thread_id": target, "message": "试图接管别人的会话"},
    )
    assert leaked.status_code == 200

    forged_thread = internal_thread_id(
        tenant_id=OTHER_TENANT,
        user_id=str(other_user_id),
        client_id=target,
    )

    async with async_session_factory() as session:
        original = await session.scalar(
            select(Conversation).where(Conversation.thread_id == target)
        )
        forged = await session.scalar(
            select(Conversation).where(Conversation.thread_id == forged_thread)
        )

    # 原会话仍归原租户；伪造者只得到自己租户下的一条新会话。
    assert original is not None
    assert original.tenant_id == TEST_TENANT
    assert forged is not None
    assert forged.tenant_id == OTHER_TENANT
    assert forged.id != original.id


async def test_artifact_is_invisible_to_other_tenant(client, other_client):
    upload = await client.put(
        "/api/v1/artifacts",
        params={"filename": "report.txt"},
        content=b"tenant-a only",
        headers={"content-type": "text/plain"},
    )

    assert upload.status_code == 200
    artifact_id = upload.json()["artifact_id"]

    own = await client.get(f"/api/v1/artifacts/{artifact_id}")
    assert own.status_code == 200

    # 跨租户：不存在（404），不是「无权访问」——不确认资源是否存在。
    foreign = await other_client.get(f"/api/v1/artifacts/{artifact_id}")
    assert foreign.status_code == 404

    foreign_content = await other_client.get(f"/api/v1/artifacts/{artifact_id}/content")
    assert foreign_content.status_code == 404

    listing = await other_client.get("/api/v1/artifacts")
    assert listing.status_code == 200
    assert artifact_id not in {item["artifact_id"] for item in listing.json()}


async def test_artifact_download_never_renders_inline(client):
    """上传 HTML 也必须按附件下载，避免同源执行脚本。"""
    upload = await client.put(
        "/api/v1/artifacts",
        params={"filename": "page.html"},
        content=b"<script>alert(1)</script>",
        headers={"content-type": "text/html"},
    )

    artifact_id = upload.json()["artifact_id"]

    response = await client.get(f"/api/v1/artifacts/{artifact_id}/content")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in response.headers["cache-control"]


async def test_admin_channel_rejects_normal_user(client):
    response = await client.get("/api/v1/admin/tenants")

    assert response.status_code == 403


async def test_admin_channel_rejects_tenant_admin_in_own_tenant(seeded_identity):
    """在自己租户里拥有管理角色不等于平台管理员。"""
    async with _as(TEST_SUBJECT, TEST_TENANT) as http:
        response = await http.get("/api/v1/admin/tenants")

    assert response.status_code == 403


async def test_admin_can_create_tenant_and_action_is_audited(admin_client):
    response = await admin_client.post(
        "/api/v1/admin/tenants",
        json={
            "tenant_id": "tenant-c",
            "name": "客户 C",
            "daily_chat_requests": 100,
            "tool_calls_per_minute": 20,
        },
    )

    assert response.status_code == 201
    assert response.json()["tenant_id"] == "tenant-c"

    # 重复创建必须是 409，不能静默覆盖配额。
    again = await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-c", "name": "客户 C"},
    )
    assert again.status_code == 409

    audit = await admin_client.get("/api/v1/admin/tenants/tenant-c/audit")

    assert audit.status_code == 200
    entries = [
        item for item in audit.json() if item["event_type"] == "admin_action"
    ]
    assert entries, "平台管理写操作必须留审计"
    assert entries[0]["resource_id"] == "tenant-c"


async def test_audit_items_carry_structured_metadata(admin_client):
    """审计条目要带回 `metadata`：没有它，「为什么会这样」只能 psql 才能回答。

    这一列回答的不是「谁在何时做了什么」（那在 `message` 里），而是「为什么」——
    拒绝原因、当时生效的上限、转交来源、改前改后。以前接口不回这一列，
    核对一次委派拒绝就得连库、还得先进租户上下文，否则被 RLS 挡成 0 行。
    """
    await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-e", "name": "客户 E"},
    )

    audit = await admin_client.get("/api/v1/admin/tenants/tenant-e/audit")

    entries = [i for i in audit.json() if i["event_type"] == "admin_action"]
    assert entries, "平台管理写操作必须留审计"

    meta = entries[0]["metadata"]
    # 空字典也算「字段在、内容是摆设」——那是另一种形式的没做。
    assert meta, "metadata 不能是空对象"
    assert meta["operation"] == "tenant_created"
    assert meta["target_tenant"] == "tenant-e"
    assert meta["actor_subject"], "至少要能答出是谁做的"


async def test_admin_writes_are_scoped_to_target_tenant(admin_client):
    """审计写在目标租户下，而不是平台租户下。"""
    await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-d", "name": "客户 D"},
    )

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.resource_id == "tenant-d")
            )
        ).all()

    assert rows
    assert {row.tenant_id for row in rows} == {"tenant-d"}


async def test_admin_can_disable_tool_for_one_tenant(admin_client, client):
    """工具策略按租户独立：关掉 A 的工具不影响 B。"""
    policy = await admin_client.put(
        f"/api/v1/admin/tenants/{TEST_TENANT}/tools/get_platform_time",
        json={"enabled": False},
    )

    assert policy.status_code == 200
    assert policy.json()["enabled"] is False

    # 恢复默认，避免影响其他用例（同一进程内共享网关）。
    restored = await admin_client.put(
        f"/api/v1/admin/tenants/{TEST_TENANT}/tools/get_platform_time",
        json={"enabled": True},
    )
    assert restored.status_code == 200
    assert restored.json()["enabled"] is True


async def test_quota_exhaustion_returns_rejected(admin_client, planner):
    """配额用尽时返回 rejected，且不落库半条会话。

    用一个全新租户，避免与其他用例共享当天的计数。
    """
    await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-q", "name": "配额租户"},
    )
    await admin_client.put(
        "/api/v1/admin/tenants/tenant-q/members",
        json={"subject": TEST_SUBJECT, "role": "tester"},
    )
    set_quota = await admin_client.put(
        "/api/v1/admin/tenants/tenant-q/quota",
        json={"daily_chat_requests": 1, "tool_calls_per_minute": 1000},
    )
    assert set_quota.status_code == 200

    planner([ActionPlan(action="chat", response="第一条。")])

    async with _as(TEST_SUBJECT, "tenant-q") as http:
        first = await http.post(
            "/api/v1/chat",
            json={"thread_id": "quota-1", "message": "第一条"},
        )
        second = await http.post(
            "/api/v1/chat",
            json={"thread_id": "quota-2", "message": "第二条"},
        )

    assert first.json()["status"] == "completed"

    body = second.json()
    assert body["status"] == "rejected"
    assert body["error_category"] == "tenant_quota_exceeded"

    async with async_session_factory() as session:
        leaked = await session.scalar(
            select(Conversation).where(Conversation.thread_id.like("%quota-2"))
        )

    assert leaked is None, "超配额请求不应留下会话记录"


async def test_member_isolation_in_admin_channel(admin_client, client):
    """把用户加进新租户后，他才能切换过去。"""
    await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-e", "name": "客户 E"},
    )

    async with _as(TEST_SUBJECT, "tenant-e") as http:
        denied = await http.get("/api/v1/me")
    assert denied.status_code == 403

    added = await admin_client.put(
        "/api/v1/admin/tenants/tenant-e/members",
        json={"subject": TEST_SUBJECT, "role": "tester"},
    )
    assert added.status_code == 200

    async with _as(TEST_SUBJECT, "tenant-e") as http:
        allowed = await http.get("/api/v1/me")

    assert allowed.status_code == 200
    body = allowed.json()
    assert body["tenant_id"] == "tenant-e"
    assert {TEST_TENANT, "tenant-e"} <= set(body["tenants"])

    removed = await admin_client.delete(
        f"/api/v1/admin/tenants/tenant-e/members/{TEST_SUBJECT}"
    )
    assert removed.status_code == 204

    async with _as(TEST_SUBJECT, "tenant-e") as http:
        denied_again = await http.get("/api/v1/me")
    assert denied_again.status_code == 403


async def test_other_user_cannot_read_own_tenant_audit(client):
    """审计属于平台管理通道，普通用户拿不到。"""
    response = await client.get(f"/api/v1/admin/tenants/{TEST_TENANT}/audit")

    assert response.status_code == 403


async def test_artifacts_are_private_within_a_tenant(client, admin_client):
    """同租户的不同用户也不能互读附件：租户隔离不等于租户内共享。"""
    upload = await client.put(
        "/api/v1/artifacts",
        params={"filename": "private.txt"},
        content=b"only for test-user",
        headers={"content-type": "text/plain"},
    )

    assert upload.status_code == 200
    artifact_id = upload.json()["artifact_id"]

    added = await admin_client.put(
        f"/api/v1/admin/tenants/{TEST_TENANT}/members",
        json={"subject": OTHER_SUBJECT, "role": "tester"},
    )
    assert added.status_code == 200

    try:
        async with _as(OTHER_SUBJECT, TEST_TENANT) as http:
            denied = await http.get(f"/api/v1/artifacts/{artifact_id}")
            listing = await http.get("/api/v1/artifacts")

        assert denied.status_code == 404
        assert artifact_id not in {item["artifact_id"] for item in listing.json()}
    finally:
        await admin_client.delete(
            f"/api/v1/admin/tenants/{TEST_TENANT}/members/{OTHER_SUBJECT}"
        )
