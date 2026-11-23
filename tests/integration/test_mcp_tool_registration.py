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
from app.models.approval import RiskLevel
from app.models.mcp_registered import McpRegisteredTool
from app.tools import mcp_tools
from app.tools.dependencies import get_tool_gateway, reset_tool_gateway
from app.tools.mcp_tools import McpToolSpec

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
    """代码登记的名字不许被配置覆盖。

    白名单现在是空的（prv_ 那批下线），所以这里临时塞一条假的进去——
    要验的是**闸门**还在，不是「现在恰好有哪些名字」。
    """
    _mock_remote(monkeypatch)
    monkeypatch.setattr(
        mcp_tools,
        "MCP_TOOL_SPECS",
        (
            McpToolSpec(
                local_name="prv_query_pressure",
                display_name="代码登记的读取工具",
                description="",
                risk_level=RiskLevel.READ,
                remote_name="queryPressure",
            ),
        ),
    )

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
    """代码登记的工具不能在控制台取消接入——否则审批路径上的实现会被绕过。"""
    _mock_remote(monkeypatch)
    monkeypatch.setattr(
        mcp_tools,
        "MCP_TOOL_SPECS",
        (
            McpToolSpec(
                local_name="prv_query_pressure",
                display_name="代码登记的读取工具",
                description="",
                risk_level=RiskLevel.READ,
                remote_name="queryPressure",
            ),
        ),
    )

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


# ---------------- 改接入项的风险等级 ----------------


async def test_update_lowers_risk_and_drops_approval(admin_client, monkeypatch):
    """把 write 降到 read，网关里那份定义必须当场跟着变。

    接入是批量做的（连接器「发现工具」一次接几十个），只能给一个统一的保守值；
    之后不让人按工具逐个降回来，「查一次压力」也要弹审批单，平台等于不可用。
    这里盯的是**两处一致**：只改库不改注册表，界面写着「读取」而实际照样
    弹审批单——两边不一致时人只会相信自己看到的那份。
    """
    _mock_remote(monkeypatch)
    created = await admin_client.post(f"{BASE}/tools", json=_payload(risk_level="write"))
    assert created.status_code == 201
    assert created.json()["requires_approval"] is True

    gateway = get_tool_gateway()
    assert gateway.get_tool(LOCAL).risk_level is RiskLevel.WRITE

    response = await admin_client.patch(f"{BASE}/tools/{LOCAL}", json={"risk_level": "read"})

    assert response.status_code == 200
    body = response.json()
    assert body["risk_level"] == "read"
    assert body["requires_approval"] is False

    definition = get_tool_gateway().get_tool(LOCAL)
    assert definition.risk_level is RiskLevel.READ
    assert definition.requires_approval is False, "只换 risk_level 会被旧的 requires_approval 顶回来"


async def test_update_requires_platform_admin(client, seeded_identity):
    response = await client.patch(f"{BASE}/tools/{LOCAL}", json={"risk_level": "read"})

    assert response.status_code == 403


async def test_update_rejects_code_registered_tool(admin_client, monkeypatch):
    """代码白名单里的风险级写在代码里，不经由配置面改。"""
    from app.api.routes import admin as admin_routes

    monkeypatch.setattr(admin_routes, "code_local_names", lambda: {"fc_querypressure"})

    response = await admin_client.patch(
        f"{BASE}/tools/fc_querypressure", json={"risk_level": "read"}
    )

    assert response.status_code == 400
    assert "代码" in response.json()["detail"]


async def test_update_rejects_empty_payload_and_missing_row(admin_client, monkeypatch):
    _mock_remote(monkeypatch)
    await admin_client.post(f"{BASE}/tools", json=_payload())

    empty = await admin_client.patch(f"{BASE}/tools/{LOCAL}", json={})
    assert empty.status_code == 400

    missing = await admin_client.patch(
        f"{BASE}/tools/not_registered_at_all", json={"risk_level": "read"}
    )
    assert missing.status_code == 404


async def test_list_reports_where_the_token_comes_from(admin_client):
    """列表要回答「令牌从哪来」，不能只说有没有。

    经连接器接入的行上 `token` 永远是空的（端点取 `connectors.base_url`、
    凭据按租户现取）。只回 `token_set=False` 时界面只能写「未配置」——而管理员
    在连接器页明明看到「已配置」，于是来问「为什么不一致」。这两种状态必须分开：
    前者是设计如此，后者是真的调不通。

    顺带盯住一处容易写错的地方：`require_platform_admin` 会把上下文租户改成
    平台租户，凭据查询要靠 `identity.tenant_id` + 显式租户条件（`INCLUDE_ALL_TENANTS`），
    不能读上下文——否则结论是「每个租户都没配凭据」。
    """
    from app.models.connector import (
        Connector,
        ConnectorAuthType,
        ConnectorCredential,
        ConnectorKind,
    )
    connector_id = None
    try:
        async with async_session_factory() as session:
            async with session.begin():
                connector = Connector(
                    name=f"t_list_{LOCAL}",
                    display_name="列表令牌来源",
                    kind=ConnectorKind.MCP,
                    base_url="http://127.0.0.1:8765/mcp",
                    auth_type=ConnectorAuthType.BEARER,
                )
                session.add(connector)
                await session.flush()
                connector_id = connector.id

                # 凭据是租户级的：admin_client 用的就是平台租户。
                # 显式给 tenant_id，不依赖上下文——它只在这段代码里有意义。
                session.add(
                    ConnectorCredential(
                        connector_id=connector.id, secret="tok", tenant_id="platform"
                    )
                )

                session.add(
                    McpRegisteredTool(
                        local_name=LOCAL,
                        remote_name=REMOTE,
                        display_name="查询日程",
                        description="按日期查日程安排。",
                        connector_id=connector.id,
                        risk_level=RiskLevel.READ,
                    )
                )

        items = (await admin_client.get(f"{BASE}/tools")).json()["items"]
        item = next(row for row in items if row["local_name"] == LOCAL)

        assert item["token_set"] is False
        assert item["token_source"] == "connector"
        assert item["connector_name"] == f"t_list_{LOCAL}"
        assert item["connector_credential_set"] is True
    finally:
        if connector_id is not None:
            from app.models.connector import ConnectorCredential as Credential

            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(Credential).where(Credential.connector_id == connector_id)
                    )
                    await session.execute(delete(Connector).where(Connector.id == connector_id))
