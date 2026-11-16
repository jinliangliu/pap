# tests/integration/test_model_providers.py
"""模型供应商管理接口：鉴权、单一启用约束、密钥不回显。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.model_provider import ModelProvider
from app.services import model_service

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/model-providers"


def _payload(name: str, **overrides) -> dict:
    body = {
        "name": name,
        "protocol": "openai-compatible",
        "api_key": "sk-secret-value",
        "base_url": "https://api.example.com",
        "models": [{"id": "m-1", "kind": "text"}, {"id": "m-2", "kind": "text"}],
        "default_text_model": "m-1",
        "temperature": 0.0,
        "reasoning_mode": "auto",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
async def _clean():
    model_service.invalidate_model_cache()
    yield
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(ModelProvider))
    model_service.invalidate_model_cache()


async def test_requires_platform_admin(client):
    response = await client.get(BASE)
    assert response.status_code in (401, 403)


async def test_create_list_and_activate(admin_client):
    first = await admin_client.post(BASE, json=_payload("DeepSeek"))
    assert first.status_code == 201, first.text
    body = first.json()
    # 首个供应商自动生效，否则用户保存完发现「没有任何一个在用」
    assert body["is_active"] is True
    assert body["api_key_set"] is True
    assert "api_key" not in body or body.get("api_key") in (None, "")

    second = await admin_client.post(BASE, json=_payload("备用网关"))
    assert second.status_code == 201
    assert second.json()["is_active"] is False

    listing = await admin_client.get(BASE)
    assert listing.status_code == 200
    data = listing.json()
    assert len(data["items"]) == 2
    assert data["effective"]["source"] == "provider"
    assert data["effective"]["model"] == "m-1"

    activated = await admin_client.post(f"{BASE}/{second.json()['id']}/activate")
    assert activated.status_code == 200

    listing = await admin_client.get(BASE)
    active = [item for item in listing.json()["items"] if item["is_active"]]
    assert len(active) == 1
    assert active[0]["name"] == "备用网关"


async def test_duplicate_name_rejected(admin_client):
    await admin_client.post(BASE, json=_payload("重名"))
    again = await admin_client.post(BASE, json=_payload("重名"))
    assert again.status_code == 409


async def test_update_keeps_key_when_blank_and_clears_on_empty(admin_client):
    created = (await admin_client.post(BASE, json=_payload("K"))).json()

    # 不传 api_key：保持原值
    updated = await admin_client.put(
        f"{BASE}/{created['id']}", json={"temperature": 0.7}
    )
    assert updated.status_code == 200
    assert updated.json()["api_key_set"] is True
    assert updated.json()["temperature"] == 0.7

    # 空串：显式清空
    cleared = await admin_client.put(f"{BASE}/{created['id']}", json={"api_key": ""})
    assert cleared.status_code == 200
    assert cleared.json()["api_key_set"] is False


async def test_delete_active_promotes_remaining(admin_client):
    first = (await admin_client.post(BASE, json=_payload("A"))).json()
    second = (await admin_client.post(BASE, json=_payload("B"))).json()
    assert first["is_active"] is True

    deleted = await admin_client.delete(f"{BASE}/{first['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["promoted"] == "B"

    listing = (await admin_client.get(BASE)).json()
    assert [item["id"] for item in listing["items"]] == [second["id"]]
    assert listing["items"][0]["is_active"] is True


async def test_discover_requires_base_url(admin_client):
    response = await admin_client.post(f"{BASE}/discover", json={})
    assert response.status_code == 400


async def test_rejects_model_outside_list(admin_client):
    body = _payload("X", default_text_model="not-in-list")
    response = await admin_client.post(BASE, json=body)
    assert response.status_code == 422


async def test_derives_default_text_model_when_omitted(admin_client):
    """不传 default_text_model 时由服务端推导。

    曾经写成 field_validator，字段取默认值就不会触发，结果是接口存下 null：
    页面显示「未设置」，实际却跑代码里的兜底模型，两边说的不是一件事。
    """
    body = _payload("自动推导")
    body.pop("default_text_model")
    body["models"] = [
        {"id": "img-1", "kind": "image"},
        {"id": "text-1", "kind": "text"},
        {"id": "text-2", "kind": "text"},
    ]

    created = await admin_client.post(BASE, json=body)
    assert created.status_code == 201
    # 跳过 image 条目，取第一个文字模型。
    assert created.json()["default_text_model"] == "text-1"


async def test_discover_surfaces_connect_timeout(admin_client, monkeypatch):
    """连接超时要把「为什么」写清楚。

    原先 f"无法访问供应商：{exc}" 遇上 httpx.ConnectTimeout 会格式化成空串
    （这个异常不带 message），页面只剩冒号，超时和拒绝连接看起来一模一样。
    """
    import httpx

    async def boom(self, url, **kwargs):
        raise httpx.ConnectTimeout("", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)

    response = await admin_client.post(
        f"{BASE}/discover", json={"base_url": "http://203.0.113.20:8083/v1"}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "连接超时" in detail
    assert "203.0.113.20:8083/v1/models" in detail
    # 内网地址要额外提示：浏览器能打开不等于服务器能打开。
    assert "内网地址" in detail


async def test_discover_reports_upstream_status(admin_client, monkeypatch):
    """供应商回 401 时带上原话和鉴权提示，不要只说「400」。"""
    import httpx

    async def unauthorized(self, url, **kwargs):
        return httpx.Response(
            401,
            json={"error": "unauthorized"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", unauthorized)

    response = await admin_client.post(
        f"{BASE}/discover", json={"base_url": "http://127.0.0.1:8083/v1"}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "供应商返回 401" in detail
    assert "unauthorized" in detail
    assert "API Key" in detail


async def test_discover_hints_tunnel_for_loopback(admin_client, monkeypatch):
    """回环地址连不上，多半是反向隧道断了，提示要指到隧道上而不是内网。"""
    import httpx

    async def boom(self, url, **kwargs):
        raise httpx.ConnectError("", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)

    response = await admin_client.post(
        f"{BASE}/discover", json={"base_url": "http://127.0.0.1:18083/v1"}
    )

    detail = response.json()["detail"]
    assert "回环地址" in detail
    assert "隧道" in detail
    assert "内网地址" not in detail
