# tests/integration/test_sandbox_pg_records.py
"""执行记录直写 PG 的控制面契约（不依赖真实 PG）。

沙箱服务在 /opt/pap-sandbox 独立 venv 里跑，import 不到主仓 ORM；
真实 SQL 由 deploy/sandbox/records.py 负责，这里用假 store 固定 server 侧的三条纪律：

1. 落库失败 → 503「结果未知」，绝不返回成功（否则审批停在 EXECUTED 且回读 404）；
2. 回读时 execution 与 state 状态不一致 → 503，不拿半截事实推进审批；
3. 正常回读带 traces + state，且同租户内按 user_id 隔离。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from deploy.sandbox import server

AUTH = {"Authorization": "Bearer test-key", "X-Sandbox-Tenant": "tenant-a"}


class FakeStore:
    def __init__(self, *, fail_save: bool = False, mismatch: bool = False) -> None:
        self.fail_save = fail_save
        self.mismatch = mismatch
        self.saved: list[dict] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save(self, *, tenant_id: str, record: dict) -> None:
        if self.fail_save:
            raise RuntimeError("PG 写入失败")
        self.saved.append({"tenant_id": tenant_id, **record})

    async def get(self, *, tenant_id: str, execution_id: str) -> dict | None:
        if not self.saved:
            return None
        record = self.saved[0]
        status = record["status"]
        return {
            "execution": {
                "execution_id": execution_id,
                "user_id": record.get("user_id", ""),
                "status": status,
                "exit_code": record.get("exit_code"),
                "cpu_ms": record.get("cpu_ms"),
                "duration_ms": record.get("duration_ms"),
                "policy_revision": None,
            },
            "traces": [
                {
                    "seq": 0,
                    "from_status": None,
                    "to_status": "queued",
                    "actor": "sandbox",
                    "detail_json": {"user_id": record.get("user_id", "")},
                },
                {
                    "seq": 1,
                    "from_status": "queued",
                    "to_status": "succeeded" if not self.mismatch else "failed",
                    "actor": "sandbox",
                    "detail_json": {"user_id": record.get("user_id", "")},
                },
            ],
            # mismatch 时 state 故意与 execution 不同：回读必须拒绝这种半截事实。
            "state": {"status": status if not self.mismatch else "running", "state_json": {}},
        }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server.settings, "backend", "process")
    monkeypatch.setattr(server.settings, "api_key", "test-key")
    monkeypatch.setattr(server.settings, "database_url", "postgresql://fake")
    # 阻止 lifespan 按配置真去连库，改成注入假 store。
    monkeypatch.setattr(server, "_build_store", lambda: None)
    server._records.clear()
    server._idempotent.clear()
    server._inflight.clear()

    with TestClient(server.app) as test_client:
        yield test_client

    server._record_store = None
    server._records.clear()


def test_save_failure_is_unknown_not_success(client, monkeypatch):
    store = FakeStore(fail_save=True)
    monkeypatch.setattr(server, "_record_store", store)

    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(42)"},
        headers={**AUTH, "X-Sandbox-User": "u1"},
    )

    assert response.status_code == 503
    assert "结果未知" in response.json()["detail"]


def test_successful_execute_persists_record(client, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(server, "_record_store", store)

    response = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(42)"},
        headers={**AUTH, "X-Sandbox-User": "u1"},
    )

    assert response.status_code == 200
    assert len(store.saved) == 1
    assert store.saved[0]["user_id"] == "u1"
    assert store.saved[0]["tenant_id"] == "tenant-a"
    assert "cpu_ms" in store.saved[0]


def test_readback_includes_traces_and_state(client, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(server, "_record_store", store)

    executed = client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(1)"},
        headers={**AUTH, "X-Sandbox-User": "u1"},
    ).json()

    readback = client.get(
        f"/v1/executions/{executed['execution_id']}",
        headers={**AUTH, "X-Sandbox-User": "u1"},
    )

    assert readback.status_code == 200
    body = readback.json()
    assert len(body["traces"]) == 2
    assert body["state"]["status"] == body["status"]


def test_readback_rejects_inconsistent_state(client, monkeypatch):
    store = FakeStore(mismatch=True)
    monkeypatch.setattr(server, "_record_store", store)

    # mismatch 时 save 依然成功，先落一条才有东西可读。
    client.post(
        "/v1/execute",
        json={"language": "python", "code": "print(1)"},
        headers={**AUTH, "X-Sandbox-User": "u1"},
    )

    response = client.get(
        "/v1/executions/exec-any",
        headers={**AUTH, "X-Sandbox-User": "u1"},
    )

    assert response.status_code == 503
    assert "不一致" in response.json()["detail"]


def test_readback_scopes_owner_within_tenant(client, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(server, "_record_store", store)

    response = client.get(
        "/v1/executions/exec-any",
        headers={**AUTH, "X-Sandbox-User": "another-user"},
    )

    assert response.status_code == 404


def test_missing_dsn_falls_back_to_memory(monkeypatch):
    monkeypatch.setattr(server.settings, "database_url", "")
    assert server._build_store() is None
