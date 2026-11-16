# tests/integration/test_mcp_tool_registration.py
"""发现之后的「接入」：能在控制台登记，也真的进得了模型目录。

这套接口是这次新开的配置面，风险点有两个，测试按这两点写：
1. 名字冲突必须挡住 —— 允许配置覆盖代码工具，等于把审批绕开的开关交出去；
2. 接入/取消接入必须和注册表同步 —— 页面显示已接入、模型却调不到，
   是这类功能最难查的故障。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.integrations.mcp_client import McpToolDescriptor
from app.models.mcp_registered import McpRegisteredTool
from app.tools.dependencies import get_tool_gateway, reset_tool_gateway

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/mcp"

REMOTE = "calendarLookup"
LOCAL = "test_calendar_lookup"


def _count():
    return select(func.count()).select_from(McpRegisteredTool)


@pytest.fixture(autouse=True)
async def _clean_rows():
    """本文件的用例会写库、改注册表，结束后都还原，避免影响其它测试。"""
    yield

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(McpRegisteredTool))

    reset_tool_gateway()


def _mock_remote(monkeypatch, tools: list[tuple[str, str]] | None = None) -> None:
    items = tools or [(REMOTE, "查询日程")]

    async def fake_list_tools(self):
        return [
            McpToolDescriptor(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": {}},
            )
            for name, description in items
        ]

    monkeypatch.setattr(
        "app.integrations.mcp_client.McpToolClient.list_tools", fake_list_tools
    )
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok"})


def _payload(**overrides) -> dict:
    body = {
        "remote_name": REMOTE,
        "local_name": LOCAL,
        "display_name": "查询日程",
        "description": "按日期查日程安排。",
        "risk_level": "read",
        "timeout_seconds": 45,
    }
    body.update(overrides)
    return body


async def test_register_requires_platform_admin(client, seeded_identity):
    response = await client.post(f"{BASE}/tools", json=_payload())

    assert response.status_code == 403


async def test_register_then_visible_and_removable(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    created = await admin_client.post(f"{BASE}/tools", json=_payload())
    assert created.status_code == 201

    body = created.json()
    assert body["local_name"] == LOCAL
    assert body["remote_name"] == REMOTE
    assert body["registered"] is True
    # 读取风险不需要审批；审批由风险等级推导，不由配置决定。
    assert body["requires_approval"] is False

    gateway = get_tool_gateway()
    assert gateway.has_tool(LOCAL)
    assert gateway.get_tool(LOCAL).source == "mcp"

    # 发现结果里这一项要标成「已接入 + 可取消」
    discovered = (await admin_client.post(f"{BASE}/discover", json={})).json()
    item = {entry["name"]: entry for entry in discovered["tools"]}[REMOTE]
    assert item["local_name"] == LOCAL
    assert item["removable"] is True

    removed = await admin_client.delete(f"{BASE}/tools/{LOCAL}")
    assert removed.status_code == 200
    assert not get_tool_gateway().has_tool(LOCAL)


async def test_register_rejects_name_from_code_whitelist(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    response = await admin_client.post(
        f"{BASE}/tools",
        json=_payload(local_name="prv_query_pressure"),
    )

    assert response.status_code == 400
    assert "代码白名单" in response.json()["detail"]


async def test_register_rejects_name_taken_by_builtin(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    response = await admin_client.post(
        f"{BASE}/tools",
        json=_payload(local_name="ocr_document"),
    )

    assert response.status_code == 400
    assert "占用" in response.json()["detail"]


async def test_register_rejects_remote_tool_that_does_not_exist(
    admin_client, monkeypatch
):
    _mock_remote(monkeypatch)

    response = await admin_client.post(
        f"{BASE}/tools",
        json=_payload(remote_name="noSuchRemoteTool"),
    )

    assert response.status_code == 400
    assert "noSuchRemoteTool" in response.json()["detail"]
    # 报错要带上查的是哪个端点：否则在第二个服务上接入时，
    # 「远端没有这个工具」会把人引去怀疑远端下线，而不是地址不对。
    assert settings.mcp_server_url in response.json()["detail"]


async def test_register_rejects_duplicate_local_name(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    first = await admin_client.post(f"{BASE}/tools", json=_payload())
    assert first.status_code == 201

    second = await admin_client.post(f"{BASE}/tools", json=_payload())

    assert second.status_code == 409


async def test_unregister_refuses_code_tool(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    response = await admin_client.delete(f"{BASE}/tools/prv_query_pressure")

    assert response.status_code == 400
    assert "代码注册" in response.json()["detail"]


async def test_unregister_unknown_returns_404(admin_client, monkeypatch):
    _mock_remote(monkeypatch)

    response = await admin_client.delete(f"{BASE}/tools/never_registered")

    assert response.status_code == 404


async def test_removing_from_table_drops_tool_from_registry(admin_client, monkeypatch):
    """取消接入后库里删行、注册表摘掉，两处不能只做一半。"""
    _mock_remote(monkeypatch)

    assert (await admin_client.post(f"{BASE}/tools", json=_payload())).status_code == 201

    async with async_session_factory() as session:
        assert await session.scalar(_count()) == 1

    await admin_client.delete(f"{BASE}/tools/{LOCAL}")

    async with async_session_factory() as session:
        assert await session.scalar(_count()) == 0
    assert not get_tool_gateway().has_tool(LOCAL)


# ---------------- 多端点 ----------------

FOREIGN = "http://127.0.0.1:8001/mcp"


def _mock_per_endpoint(monkeypatch, by_url: dict[str, list[tuple[str, str]]]) -> None:
    """按端点返回不同清单：模拟「两个 MCP 服务各有各的工具」。"""

    async def fake_fetch(*, url="", token="", timeout_seconds=None):
        key = url or settings.mcp_server_url
        return {
            name: McpToolDescriptor(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": {}},
            )
            for name, description in by_url.get(key, [])
        }

    monkeypatch.setattr("app.api.routes.admin.fetch_remote_tools", fake_fetch)
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")


async def test_register_verifies_against_the_endpoint_it_will_call(admin_client, monkeypatch):
    """工具只存在于第二个服务上：带着那个端点接入必须成功，且记录下端点。

    这正是实际踩到的坑——发现照 A 地址做，接入却拿 .env 的 B 地址校验，
    报出来是「远端没有这个工具」，工具其实在。
    """
    _mock_per_endpoint(
        monkeypatch,
        {FOREIGN: [("ocr_file", "识别文件文字")]},
    )

    # 不传端点：校验的是 .env 默认端点，那里没有这个工具
    wrong = await admin_client.post(f"{BASE}/tools", json=_payload(remote_name="ocr_file"))
    assert wrong.status_code == 400
    assert FOREIGN not in wrong.json()["detail"]

    ok = await admin_client.post(
        f"{BASE}/tools",
        json=_payload(remote_name="ocr_file", server_url=FOREIGN),
    )
    assert ok.status_code == 201

    body = ok.json()
    assert body["server_url"] == FOREIGN
    # 外部端点没登记令牌：接口只回「有没有」，且这里是没有
    assert body["token_set"] is False

    async with async_session_factory() as session:
        row = await session.scalar(
            select(McpRegisteredTool).where(McpRegisteredTool.local_name == LOCAL)
        )
    assert row is not None and row.server_url == FOREIGN


async def test_discover_reports_which_endpoint_it_used(admin_client, monkeypatch):
    """发现结果要带回端点，前端才能拿同一个地址去接入。"""
    _mock_per_endpoint(monkeypatch, {FOREIGN: [("ocr_file", "")]})

    body = (await admin_client.post(f"{BASE}/discover", json={"url": FOREIGN})).json()

    assert body["server_url"] == FOREIGN
    # 另一个服务上的同名工具不该被标成「已接入」：代码白名单只属于默认端点
    assert body["tools"][0]["local_name"] is None
    assert body["tools"][0]["removable"] is False


async def test_unregistered_tool_is_not_marked_removable(admin_client, monkeypatch):
    """未接入的工具两侧映射都是空，不能因为「空 == 空」就给出取消按钮。"""
    _mock_remote(monkeypatch)

    body = (await admin_client.post(f"{BASE}/discover", json={})).json()
    item = {entry["name"]: entry for entry in body["tools"]}[REMOTE]

    assert item["local_name"] is None
    assert item["removable"] is False
