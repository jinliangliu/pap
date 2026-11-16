# tests/integration/test_auth_mode.py
"""认证模式必须真正生效。

历史缺陷：auth_mode 只是配置项，代码里没有任何分支使用它。
结果是服务无条件信任 X-Authenticated-Subject——一旦经反代暴露到公网，
任何人带上这个头就能冒充任意用户。
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app


def _client(headers: dict[str, str] | None = None) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers or {},
    )


async def test_gateway_mode_rejects_missing_identity(monkeypatch, planner):
    """gateway 模式下缺少身份头必须 401，不得回落到开发主体。"""
    from app.agents.schemas import ActionPlan

    monkeypatch.setattr(settings, "auth_mode", "gateway")
    planner([ActionPlan(action="chat", response="不应被调用。")])

    async with _client() as client:
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "auth-gw-1", "message": "你好"},
        )

    assert response.status_code == 401
    assert "身份" in response.json()["detail"]


async def test_dev_mode_falls_back_to_dev_subject(monkeypatch, planner, seeded_identity):
    """dev 模式下允许缺省身份，便于本地调试。"""
    from app.agents.schemas import ActionPlan

    monkeypatch.setattr(settings, "auth_mode", "dev")
    monkeypatch.setattr(settings, "dev_subject", "test-user")
    planner([ActionPlan(action="chat", response="已收到。")])

    async with _client() as client:
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "auth-dev-1", "message": "你好"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_gateway_mode_does_not_provision_unknown_subject(monkeypatch, planner):
    """gateway 模式下未知主体不得自动建号，否则反代失误即可自造身份。"""
    from app.agents.schemas import ActionPlan

    monkeypatch.setattr(settings, "auth_mode", "gateway")
    planner([ActionPlan(action="chat", response="不应被调用。")])

    async with _client({"X-Authenticated-Subject": "ghost-user"}) as client:
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "auth-gw-2", "message": "你好"},
        )

    assert response.status_code == 401


async def test_gateway_mode_accepts_known_subject(monkeypatch, planner, seeded_identity):
    """由反代注入的已知主体正常放行。"""
    from app.agents.schemas import ActionPlan

    monkeypatch.setattr(settings, "auth_mode", "gateway")
    planner([ActionPlan(action="chat", response="已收到。")])

    async with _client({"X-Authenticated-Subject": "test-user"}) as client:
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "auth-gw-3", "message": "你好"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
