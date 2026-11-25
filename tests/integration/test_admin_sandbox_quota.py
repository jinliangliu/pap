# tests/integration/test_admin_sandbox_quota.py
"""沙箱事务配额的管理通道：谁能看、谁能改、会不会串租户。

这三项配额住在 tenant_quotas 里，但它们是平台运维口径（单机并发、CPU 成本、
磁盘上限），不是租户自己能调的业务参数。所以只走 /api/v1/admin/*，
普通身份的 403 是硬闸，不是前端把字段藏起来就算数。
"""

from __future__ import annotations

from tests.conftest import TEST_TENANT

_SANDBOX_FIELDS = {
    "max_concurrent_tasks": 4,
    "daily_cpu_seconds": 3600,
    "workspace_max_bytes": 256 * 1024 * 1024,
}
_BASE_FIELDS = {
    "daily_chat_requests": 1000,
    "tool_calls_per_minute": 60,
    "knowledge_max_documents": 500,
}


async def test_admin_can_set_and_read_sandbox_quota(admin_client):
    created = await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-sandbox", "name": "沙箱配额租户"},
    )
    assert created.status_code in (200, 201)

    body = {**_BASE_FIELDS, **_SANDBOX_FIELDS}
    saved = await admin_client.put(
        "/api/v1/admin/tenants/tenant-sandbox/quota", json=body
    )
    assert saved.status_code == 200
    for key, value in body.items():
        assert saved.json()[key] == value, key

    # 另开一次请求读取：回显必须来自库，不能只是把 PUT 的请求体原样吐回来。
    read = await admin_client.get("/api/v1/admin/tenants/tenant-sandbox/quota")
    assert read.status_code == 200
    for key, value in body.items():
        assert read.json()[key] == value, key


async def test_reading_quota_does_not_create_row(admin_client):
    await admin_client.post(
        "/api/v1/admin/tenants",
        json={"tenant_id": "tenant-noquota", "name": "未配配额"},
    )
    read = await admin_client.get("/api/v1/admin/tenants/tenant-noquota/quota")
    assert read.status_code == 200
    assert read.json()["max_concurrent_tasks"] == 0
    assert read.json()["daily_cpu_seconds"] == 0
    assert read.json()["workspace_max_bytes"] == 0


async def test_normal_tenant_cannot_read_or_write_sandbox_quota(client):
    path = f"/api/v1/admin/tenants/{TEST_TENANT}/quota"
    read = await client.get(path)
    assert read.status_code == 403

    write = await client.put(
        path, json={**_BASE_FIELDS, **_SANDBOX_FIELDS}
    )
    assert write.status_code == 403


async def test_sandbox_quota_is_not_shared_between_tenants(admin_client):
    for tenant_id in ("tenant-sbx-a", "tenant-sbx-b"):
        created = await admin_client.post(
            "/api/v1/admin/tenants",
            json={"tenant_id": tenant_id, "name": tenant_id},
        )
        assert created.status_code in (200, 201)

    saved = await admin_client.put(
        "/api/v1/admin/tenants/tenant-sbx-a/quota",
        json={**_BASE_FIELDS, **_SANDBOX_FIELDS},
    )
    assert saved.status_code == 200

    other = await admin_client.get("/api/v1/admin/tenants/tenant-sbx-b/quota")
    assert other.status_code == 200
    assert other.json()["max_concurrent_tasks"] == 0
    assert other.json()["daily_cpu_seconds"] == 0
    assert other.json()["workspace_max_bytes"] == 0
