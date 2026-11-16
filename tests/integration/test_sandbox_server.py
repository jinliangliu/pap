# tests/integration/test_sandbox_server.py
"""沙箱服务的多租户行为：鉴权、租户隔离、配额、幂等。

这些用例跑在 `process` 后端上——它们验证的是控制面逻辑，
不是隔离强度。隔离强度由 tests/unit/test_sandbox_runtime.py 固定。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from deploy.sandbox import server

AUTH = {"Authorization": "Bearer test-key", "X-Sandbox-Tenant": "tenant-a"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server.settings, "backend", "process")
    monkeypatch.setattr(server.settings, "api_key", "test-key")
    monkeypatch.setattr(server.settings, "max_code_bytes", 64_000)
    monkeypatch.setattr(server.settings, "max_output_chars", 200_000)

    server._records.clear()
    server._idempotent.clear()
    server._inflight.clear()

    with TestClient(server.app) as test_client:
        yield test_client

    server._records.clear()
    server._idempotent.clear()
    server._inflight.clear()


def test_health_reports_process_backend_without_isolation(client):
    response = client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["isolation"] == "none"
    assert body["backend"] == "process"


def test_execute_requires_api_key(client):
    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(1)"},
        headers={"X-Sandbox-Tenant": "tenant-a"},
    )

    assert response.status_code == 401


def test_execute_rejects_wrong_api_key(client):
    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(1)"},
        headers={"Authorization": "Bearer wrong", "X-Sandbox-Tenant": "tenant-a"},
    )

    assert response.status_code == 401


def test_execute_rejects_invalid_tenant(client):
    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(1)"},
        headers={"Authorization": "Bearer test-key", "X-Sandbox-Tenant": "bad tenant"},
    )

    assert response.status_code == 400


def test_execute_rejects_unknown_language(client):
    response = client.post(
        "/v1/execute",
        json={"language": "bash", "code": "id"},
        headers=AUTH,
    )

    assert response.status_code == 400


def test_execute_rejects_oversized_code(client, monkeypatch):
    monkeypatch.setattr(server.settings, "max_code_bytes", 10)

    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print('x' * 100)"},
        headers=AUTH,
    )

    assert response.status_code == 413


def test_execution_record_is_scoped_to_tenant(client):
    created = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print('tenant-a')"},
        headers=AUTH,
    )

    assert created.status_code == 200
    execution_id = created.json()["execution_id"]

    own = client.get(
        f"/v1/executions/{execution_id}",
        headers=AUTH,
    )
    assert own.status_code == 200
    assert own.json()["stdout"].strip() == "tenant-a"

    other = client.get(
        f"/v1/executions/{execution_id}",
        headers={"Authorization": "Bearer test-key", "X-Sandbox-Tenant": "tenant-b"},
    )
    # 404 而不是 403：不能泄露"这个 ID 存在"。
    assert other.status_code == 404


def test_idempotency_key_returns_same_execution(client):
    payload = {"language": "python", "code": "print('once')"}
    headers = {**AUTH, "Idempotency-Key": "approval:t1:r1"}

    first = client.post("/v1/execute", json=payload, headers=headers)
    second = client.post("/v1/execute", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["execution_id"] == second.json()["execution_id"]


async def test_quota_limits_each_tenant_independently():
    quota = server.Quota(max_concurrent=4, max_per_tenant=1)

    await quota.acquire("tenant-a", timeout=0.1)

    with pytest.raises(server.QuotaExceeded):
        await quota.acquire("tenant-a", timeout=0.05)

    # 另一个租户不被邻居的额度拖住。
    await quota.acquire("tenant-b", timeout=0.05)

    quota.release("tenant-a")
    quota.release("tenant-b")

    assert quota.active == 0


async def test_quota_enforces_global_cap():
    quota = server.Quota(max_concurrent=1, max_per_tenant=1)

    await quota.acquire("tenant-a", timeout=0.1)

    with pytest.raises(server.QuotaExceeded):
        await quota.acquire("tenant-b", timeout=0.05)

    quota.release("tenant-a")
    assert quota.active == 0
