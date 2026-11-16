# tests/integration/test_custom_tools_api.py
"""自定义 HTTP Tool 的管理接口。

这里盯四件事：
1. 只有 platform_admin 能读写；
2. 保存后必须真的进网关注册表（否则页面显示已保存、模型却看不见）；
3. 停用 / 删除必须从注册表摘掉；
4. 内置工具名不允许被配置覆盖。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.audit import AuditLog
from app.models.custom_tool import CustomTool
from app.services.custom_tool_service import unregister_row
from app.tools.dependencies import get_tool_gateway

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/tools"


def _payload(name: str = "e2e_order_query", **overrides) -> dict:
    data = {
        "name": name,
        "display_name": "工单查询",
        "description": "按站点查询工单",
        "method": "GET",
        "url": "https://api.example.com/stations/{station_id}/orders",
        "risk_level": "read",
        "timeout_seconds": 15,
        "params": [
            {"name": "station_id", "location": "path", "type": "string", "required": True},
            {"name": "limit", "location": "query", "type": "integer", "required": False},
        ],
        "headers": {},
        "enabled": True,
    }
    data.update(overrides)
    return data


@pytest.fixture
async def clean_custom_tools():
    """每个用例后清库并注销，避免污染共用网关。"""
    yield

    gateway = get_tool_gateway()
    async with async_session_factory() as session:
        async with session.begin():
            names = (
                await session.scalars(select(CustomTool.name))
            ).all()
            await session.execute(delete(CustomTool))

    for name in names:
        unregister_row(gateway, name)


async def test_requires_platform_admin(client, seeded_identity):
    assert (await client.get(BASE)).status_code == 403
    assert (await client.post(BASE, json=_payload())).status_code == 403
    assert (await client.get(f"{BASE}/export")).status_code == 403


async def test_create_registers_tool_and_exposes_source(
    admin_client, client, clean_custom_tools
):
    response = await admin_client.post(BASE, json=_payload())
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "e2e_order_query"
    assert body["registered"] is True
    assert body["requires_approval"] is False
    assert body["input_schema"]["required"] == ["station_id"]

    # 普通用户的工具目录里也要出现，且来源是 custom。
    tools = (await client.get("/api/v1/tools")).json()
    created = [t for t in tools if t["name"] == "e2e_order_query"]
    assert len(created) == 1
    assert created[0]["source"] == "custom"
    assert created[0]["kind"] == "http"


async def test_write_risk_requires_approval(admin_client, clean_custom_tools):
    response = await admin_client.post(BASE, json=_payload(risk_level="write"))
    assert response.status_code == 201
    assert response.json()["requires_approval"] is True


async def test_duplicate_name_conflicts(admin_client, clean_custom_tools):
    assert (await admin_client.post(BASE, json=_payload())).status_code == 201
    second = await admin_client.post(BASE, json=_payload())
    assert second.status_code == 409


async def test_cannot_shadow_builtin_tool(admin_client, clean_custom_tools):
    builtin = get_tool_gateway().list_tools()[0]
    response = await admin_client.post(
        BASE,
        json=_payload(name=builtin.name, url="https://api.example.com/x", params=[]),
    )

    assert response.status_code == 400
    assert "占用" in response.json()["detail"]


async def test_url_placeholder_without_param_is_rejected(
    admin_client, clean_custom_tools
):
    response = await admin_client.post(
        BASE,
        json=_payload(params=[], url="https://api.example.com/orders/{order_id}"),
    )

    assert response.status_code == 400
    assert "order_id" in response.json()["detail"]


async def test_disable_removes_from_catalog(admin_client, client, clean_custom_tools):
    created = (await admin_client.post(BASE, json=_payload())).json()
    assert any(t["name"] == created["name"] for t in (await client.get("/api/v1/tools")).json())

    updated = await admin_client.put(f"{BASE}/{created['id']}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["registered"] is False

    tools = (await client.get("/api/v1/tools")).json()
    assert all(t["name"] != created["name"] for t in tools)


async def test_update_url_and_params(admin_client, client, clean_custom_tools):
    created = (await admin_client.post(BASE, json=_payload())).json()

    updated = await admin_client.put(
        f"{BASE}/{created['id']}",
        json={
            "url": "https://api.example.com/orders",
            "params": [],
            "display_name": "工单查询 v2",
        },
    )

    assert updated.status_code == 200
    body = updated.json()
    assert body["url"] == "https://api.example.com/orders"
    assert body["params"] == []
    assert body["display_name"] == "工单查询 v2"

    tool = [t for t in (await client.get("/api/v1/tools")).json() if t["name"] == created["name"]][0]
    assert tool["display_name"] == "工单查询 v2"
    assert tool["input_schema"]["properties"] == {}


async def test_delete_unregisters(admin_client, client, clean_custom_tools):
    created = (await admin_client.post(BASE, json=_payload())).json()

    deleted = await admin_client.delete(f"{BASE}/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    assert get_tool_gateway().has_tool(created["name"]) is False
    assert all(
        t["name"] != created["name"] for t in (await client.get("/api/v1/tools")).json()
    )


async def test_export_then_import_merge_and_overwrite(admin_client, clean_custom_tools):
    created = (await admin_client.post(BASE, json=_payload())).json()

    exported = (await admin_client.get(f"{BASE}/export")).json()
    assert exported["version"] == 1
    names = [t["name"] for t in exported["tools"]]
    assert created["name"] in names

    tool_def = [t for t in exported["tools"] if t["name"] == created["name"]][0]

    # merge：同名跳过。
    merged = await admin_client.post(
        BASE + "/import", json={"tools": [tool_def], "mode": "merge"}
    )
    assert merged.json()["skipped"] == 1
    assert merged.json()["created"] == 0

    # overwrite：同名更新。
    tool_def["display_name"] = "导入后改名"
    overwritten = await admin_client.post(
        BASE + "/import", json={"tools": [tool_def], "mode": "overwrite"}
    )
    payload = overwritten.json()
    assert payload["updated"] == 1
    assert payload["items"][0]["action"] == "updated"

    rows = (await admin_client.get(BASE)).json()["items"]
    row = [r for r in rows if r["name"] == created["name"]][0]
    assert row["display_name"] == "导入后改名"


async def test_import_skips_builtin_name(admin_client, clean_custom_tools):
    builtin = get_tool_gateway().list_tools()[0].name
    payload = _payload(name=builtin, url="https://api.example.com/x", params=[])

    result = (
        await admin_client.post(BASE + "/import", json={"tools": [payload], "mode": "merge"})
    ).json()

    assert result["skipped"] == 1
    assert "同名" in result["items"][0]["message"]


async def test_parse_url_extracts_placeholders(admin_client):
    response = await admin_client.post(
        BASE + "/parse-url",
        json={"url": "https://api.example.com/stations/{station_id}/orders/:order_id"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://api.example.com/stations/{station_id}/orders/{order_id}"
    assert sorted(p["name"] for p in body["params"]) == ["order_id", "station_id"]


async def test_parse_curl_extracts_method_and_body(admin_client):
    curl = (
        "curl -X POST 'https://api.example.com/orders/{order_id}/notes' "
        "-H 'Content-Type: application/json' "
        "-H 'Authorization: Bearer secret' "
        "-d '{\"note\": \"现场\", \"urgent\": true}'"
    )

    response = await admin_client.post(BASE + "/parse-curl", json={"curl": curl})

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "POST"
    assert body["url"] == "https://api.example.com/orders/{order_id}/notes"
    assert body["dropped_headers"] == ["Authorization"]
    locations = {p["name"]: p["location"] for p in body["params"]}
    assert locations["order_id"] == "path"
    assert locations["note"] == "body"
    assert locations["urgent"] == "body"


async def test_parse_curl_rejects_credentials_flag(admin_client):
    response = await admin_client.post(
        BASE + "/parse-curl",
        json={"curl": "curl -u user:pass https://api.example.com/x"},
    )

    assert response.status_code == 400
    assert "凭据" in response.json()["detail"]


async def test_writes_are_audited(admin_client, clean_custom_tools):
    created = (await admin_client.post(BASE, json=_payload())).json()

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.resource_type == "custom_tool")
            )
        ).all()

    actions = {(row.message, row.resource_id) for row in rows}
    assert ("custom_tool_created", str(created["id"])) in actions
