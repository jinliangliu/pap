# tests/integration/test_connectors_api.py
"""连接器 HTTP 层：权限切分、凭据只进不出、审计可查。

这份文件盯的是「接口层会不会把不该给的东西给出去」：
凭据回显、越权改定义、删连接器把工具变成孤儿、审计没落库。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.audit import AuditLog
from app.models.connector import Connector, ConnectorCredential
from app.tenancy.context import tenant_scope

BASE = "/api/v1/connectors"
TENANT = "default"
PLATFORM = "platform"


def _name() -> str:
    return f"conn_{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def clean_connectors():
    """清干净三张表。

    只删 `connectors` 不够：`custom_tools` / `mcp_registered_tools` 里的
    `connector_id` 没有外键约束，删了父行会留下指向旧 id 的引用。SQLite 的
    自增 id 会被复用，于是新连接器"凭空"多了几个工具——而报错是
    「还有 1 个工具挂着」，查起来毫无头绪。
    """
    from app.models.custom_tool import CustomTool
    from app.models.mcp_registered import McpRegisteredTool

    async with async_session_factory() as session:
        with tenant_scope(PLATFORM):
            await session.execute(delete(ConnectorCredential))
            await session.execute(delete(McpRegisteredTool))
            await session.execute(delete(CustomTool))
            await session.execute(delete(Connector))
            await session.commit()
    yield


async def _create(admin_client, **overrides) -> dict:
    payload = {
        "name": _name(),
        "display_name": "飞书",
        "description": "",
        "kind": "mcp",
        "base_url": "https://mcp.example.com/mcp",
        "auth_type": "bearer",
        "timeout_seconds": 30,
    }
    payload.update(overrides)

    response = await admin_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_non_admin_cannot_change_definitions(client, clean_connectors):
    """连接器定义是平台级的：改一次影响所有租户的端点。

    租户成员能改的话，一个人把 base_url 指向自己的服务，全平台的数据
    就都流到那里去了。
    """
    response = await client.post(
        BASE,
        json={
            "name": _name(),
            "kind": "http",
            "base_url": "https://api.example.com",
        },
    )

    assert response.status_code == 403


async def test_admin_creates_connector_and_every_tenant_sees_it(
    admin_client, client, clean_connectors
):
    """定义是平台级的，所以别的租户看得见；但「凭据配没配」按租户算。"""
    created = await _create(admin_client)

    response = await client.get(BASE)
    assert response.status_code == 200

    items = {item["name"]: item for item in response.json()["items"]}
    assert created["name"] in items
    assert items[created["name"]]["credential_set"] is False
    assert items[created["name"]]["tool_count"] == 0


async def test_credential_is_write_only(admin_client, client, clean_connectors):
    """配完凭据之后，任何响应里都不能出现明文。

    这一类泄漏最难发现：接口看起来正常，只是多了一个字段。
    所以断言不是「有没有某个字段」，而是「整段响应里有没有那串字符」。
    """
    created = await _create(admin_client)
    secret = "sk-live-9f2b7c1d4e"

    response = await client.put(
        f"{BASE}/{created['name']}/credential",
        json={"secret": secret},
    )
    assert response.status_code == 200
    assert secret not in response.text

    detail = await client.get(f"{BASE}/{created['name']}")
    assert detail.status_code == 200
    assert secret not in detail.text
    assert detail.json()["credential_set"] is True

    listing = await client.get(BASE)
    assert secret not in listing.text


async def test_credential_write_is_audited_without_the_value(
    admin_client, client, clean_connectors
):
    """凭据写入要留痕，但只记「设置过」。

    审计表长期保留，一个令牌写进去就等于永久泄露——而且是最容易被
    备份、导出、贴进工单的那张表。
    """
    created = await _create(admin_client)
    secret = "sk-audit-should-not-appear"

    await client.put(f"{BASE}/{created['name']}/credential", json={"secret": secret})

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            rows = (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.event_type == "connector_credential_set"
                    )
                )
            ).all()

    assert rows, "凭据写入没有落审计"
    for row in rows:
        assert secret not in str(row.metadata_json)
        assert secret not in (row.message or "")


async def test_connector_creation_is_audited(admin_client, clean_connectors):
    created = await _create(admin_client)

    async with async_session_factory() as session:
        with tenant_scope(PLATFORM):
            rows = (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.event_type == "connector_created",
                    )
                )
            ).all()

    # 按名字匹配而不是按主键：审计表不清，自增 id 会在用例之间复用。
    matched = [row for row in rows if row.metadata_json.get("name") == created["name"]]

    assert len(matched) == 1
    assert matched[0].resource_id == str(created["id"])


async def test_delete_is_blocked_while_tools_are_bound(
    admin_client, clean_connectors
):
    """下面还挂着工具时，删连接器必须是 400 而不是「顺手把工具改成孤儿」。

    静默改配置含义是最坏的一种"成功"：接口回 200，工具从「用连接器凭据」
    变成「用 .env 的令牌」，下一次调用打到哪、用谁的身份，没人说得清。
    """
    from app.models.mcp_registered import McpRegisteredTool

    created = await _create(admin_client)

    async with async_session_factory() as session:
        with tenant_scope(PLATFORM):
            session.add(
                McpRegisteredTool(
                    local_name=f"tool_{uuid.uuid4().hex[:8]}",
                    remote_name="remote_x",
                    connector_id=created["id"],
                    display_name="x",
                    description="",
                )
            )
            await session.commit()

    response = await admin_client.delete(f"{BASE}/{created['name']}")

    assert response.status_code == 400
    assert "工具" in response.json()["detail"]


async def test_delete_works_when_nothing_is_bound(admin_client, clean_connectors):
    created = await _create(admin_client)

    response = await admin_client.delete(f"{BASE}/{created['name']}")
    assert response.status_code == 200

    assert (await admin_client.get(f"{BASE}/{created['name']}")).status_code == 404


async def test_health_check_without_credential_says_so(admin_client, clean_connectors):
    """没配凭据就检查，必须直接说「没配凭据」。

    报「检测失败」会让人去查网络、查对端，而真正要做的事是去填一个输入框。
    """
    created = await _create(admin_client)

    response = await admin_client.post(f"{BASE}/{created['name']}/check")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "凭据" in body["message"]


async def test_unknown_auth_scheme_is_rejected_early(admin_client, clean_connectors):
    """MCP 只会发 Bearer。允许配自定义头，会让「填了凭据还是 401」变成一个
    查不动的配置问题。"""
    response = await admin_client.post(
        BASE,
        json={
            "name": _name(),
            "kind": "mcp",
            "base_url": "https://mcp.example.com/mcp",
            "auth_type": "header",
            "auth_header_name": "X-Api-Key",
        },
    )

    assert response.status_code == 400
    assert "Bearer" in response.json()["detail"]


async def test_duplicate_name_conflicts(admin_client, clean_connectors):
    created = await _create(admin_client)
    response = await admin_client.post(
        BASE,
        json={"name": created["name"], "kind": "http", "base_url": ""},
    )
    assert response.status_code == 400


async def test_delete_also_purges_stored_credentials(admin_client, clean_connectors):
    """删连接器要把它在各租户下的凭据一起清掉。

    凭据表没有外键（SQLite 上外键是空操作，测试全绿不代表生产也对），
    清理只能靠显式删除。同时这条用例覆盖了清理的**位置**：它必须在
    租户上下文之外执行，放进去会抛 `TenantIsolationError`，
    表现成「连接器已删、接口 500」。
    """
    created = await _create(admin_client)

    response = await admin_client.put(
        f"{BASE}/{created['name']}/credential", json={"secret": "tok-1"}
    )
    assert response.status_code == 200, response.text

    # 凭据写在调用者自己的租户下（这里是平台租户），删除时也只清这一份：
    # 别的租户的行跨不过 RLS，交给维护任务（见 connector_service）。
    async with async_session_factory() as session:
        with tenant_scope(PLATFORM):
            assert (
                await session.scalar(
                    select(ConnectorCredential).where(
                        ConnectorCredential.connector_id == created["id"]
                    )
                )
                is not None
            )

    response = await admin_client.delete(f"{BASE}/{created['name']}")
    assert response.status_code == 200, response.text

    async with async_session_factory() as session:
        with tenant_scope(PLATFORM):
            assert (
                await session.scalar(
                    select(ConnectorCredential).where(
                        ConnectorCredential.connector_id == created["id"]
                    )
                )
                is None
            )
