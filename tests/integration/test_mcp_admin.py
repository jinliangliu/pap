# tests/integration/test_mcp_admin.py
"""MCP 管理接口：只读现状 + 远端工具发现。

发现接口会让服务端拿管理员给的地址去外连，所以这里重点盯三件事：
鉴权、缺参数时不乱连、以及「发现 ≠ 接入」这层映射没搞错。
"""

from __future__ import annotations

import pytest

from app.api.routes import admin as admin_routes
from app.core.config import settings
from app.integrations.mcp_client import McpToolDescriptor, McpUnavailableError

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/mcp"


async def test_requires_platform_admin(client, seeded_identity):
    response = await client.get(BASE)
    assert response.status_code == 403


async def test_runtime_reports_config_without_leaking_token(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_enabled", True)
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok-secret"})

    payload = (await admin_client.get(BASE)).json()

    assert payload["enabled"] is True
    assert payload["server_url"] == "http://127.0.0.1:8765/mcp"
    assert payload["token_configured"] is True
    assert payload["token_tenants"] == ["default"]
    # 令牌本身绝不能出现在响应里。
    assert "tok-secret" not in str(payload)


async def test_discover_needs_url(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_server_url", None)
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok"})

    response = await admin_client.post(f"{BASE}/discover", json={})

    assert response.status_code == 400
    assert "URL" in response.json()["detail"]


async def test_discover_needs_token(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {})

    response = await admin_client.post(f"{BASE}/discover", json={})

    assert response.status_code == 400
    assert "令牌" in response.json()["detail"]


async def test_discover_marks_registered_tools(admin_client, monkeypatch):
    """已登记的远端工具要带出本地名，未登记的不带 —— 这是「发现≠接入」。"""
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok"})

    # 代码白名单已空（prv_ 那批下线），映射只能来自接入表；这里直接给一条，
    # 验的是「已登记的带本地名、未登记的不带」这条语义，不是映射从哪来。
    registered_remote = "queryPressure"
    monkeypatch.setattr(
        admin_routes,
        "_registered_remote_map",
        lambda: {registered_remote: "fc_querypressure"},
    )

    async def fake_list_tools(self):
        return [
            McpToolDescriptor(name=registered_remote, description="已接入的", input_schema={}),
            McpToolDescriptor(name="notRegisteredTool", description="未接入的", input_schema={}),
        ]

    monkeypatch.setattr(
        "app.integrations.mcp_client.McpToolClient.list_tools", fake_list_tools
    )

    payload = (await admin_client.post(f"{BASE}/discover", json={})).json()

    by_name = {item["name"]: item for item in payload["tools"]}
    assert by_name[registered_remote]["local_name"]
    assert by_name["notRegisteredTool"]["local_name"] is None
    assert payload["count"] == 2
    assert payload["registered_count"] == 1


async def test_discover_surfaces_connection_failure(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok"})

    async def boom(self):
        raise McpUnavailableError("connection refused")

    monkeypatch.setattr("app.integrations.mcp_client.McpToolClient.list_tools", boom)

    response = await admin_client.post(f"{BASE}/discover", json={})

    assert response.status_code == 400
    assert "connection refused" in response.json()["detail"]
