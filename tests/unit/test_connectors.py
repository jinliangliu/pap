# tests/unit/test_connectors.py
"""连接器：凭据隔离、边界语义、失败可行动。

这份文件只钉那些「错了会静默给出错误结论」的点：
- 凭据串租户：A 的令牌被 B 用掉，返回值看起来完全正常；
- 凭据缺失静默放行：远端当匿名处理，返回空列表，模型编一个「没有数据」；
- 停用连接器仍可调用：页面显示已停用，调用照样打到外部系统；
- 边界只认工具名：接一个连接器要改二十处，改漏一处表现为「它说没这个能力」。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.approval import RiskLevel
from app.models.connector import Connector, ConnectorAuthType, ConnectorCredential, ConnectorKind
from app.services import connector_service
from app.tenancy.context import tenant_scope
from app.tools.gateway import ToolGateway
from app.tools.registry import ToolRegistry
from app.tools.types import (
    ConnectorRuntime,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

TENANT = "default"
OTHER = "other"


@pytest.fixture(autouse=True)
def _clear_cache():
    connector_service.invalidate_runtime()
    yield
    connector_service.invalidate_runtime()


async def _make_connector(
    *,
    name: str | None = None,
    kind: ConnectorKind = ConnectorKind.HTTP,
    base_url: str = "https://api.example.com",
    auth_type: ConnectorAuthType = ConnectorAuthType.BEARER,
    enabled: bool = True,
) -> int:
    # 名字全局唯一，而测试库是会话级建表、用例之间不清理。
    # 用随机后缀而不是固定名字，避免「第二个用例撞名」这种与实现无关的失败。
    clean = name or f"conn_{uuid.uuid4().hex[:10]}"

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await connector_service.create_connector(
                session,
                name=clean,
                display_name=clean,
                description="",
                kind=kind,
                base_url=base_url,
                auth_type=auth_type,
                auth_header_name="",
                timeout_seconds=20,
                actor_id="tester",
            )
            row.enabled = enabled
            await session.commit()
            return row.id


async def _set_secret(connector_id: int, *, tenant: str, secret: str) -> None:
    async with async_session_factory() as session:
        with tenant_scope(tenant):
            row = await session.get(Connector, connector_id)
            assert row is not None
            await connector_service.set_credential(
                session,
                connector=row,
                tenant_id=tenant,
                secret=secret,
                username=None,
                actor_id="tester",
            )
            await session.commit()


# ---------------- 配置校验 ----------------


def test_connector_name_must_be_stable_identifier():
    """名字会进智能体边界与审计，不能是随手的展示名。

    允许中文/空格的话，「在边界里引用它」就变成一件需要转义的事，
    而转义出错的表现是「配了但没生效」。
    """
    for bad in ("飞书", "Feishu", "a", "has space", "1abc"):
        with pytest.raises(connector_service.ConnectorConfigError):
            connector_service.validate_name(bad)

    assert connector_service.validate_name("feishu_prod") == "feishu_prod"


def test_mcp_connector_rejects_auth_types_it_cannot_send():
    """MCP 只会发 Authorization: Bearer。

    允许配 header/basic 的后果是「凭据明明填了还是 401」——
    所有配置看起来都对，只有抓包能看出来，这类错最难查。
    """
    with pytest.raises(connector_service.ConnectorConfigError):
        connector_service.validate_auth(
            ConnectorKind.MCP, ConnectorAuthType.HEADER, "X-Api-Key"
        )

    with pytest.raises(connector_service.ConnectorConfigError):
        connector_service.validate_auth(ConnectorKind.MCP, ConnectorAuthType.BASIC, "")

    # HTTP 连接器可以用自定义头。
    assert (
        connector_service.validate_auth(
            ConnectorKind.HTTP, ConnectorAuthType.HEADER, "X-Api-Key"
        )
        == "X-Api-Key"
    )


def test_mcp_connector_requires_endpoint():
    with pytest.raises(connector_service.ConnectorConfigError):
        connector_service.validate_base_url(ConnectorKind.MCP, "")

    # HTTP 连接器允许留空（各工具写完整 URL，v1 行为）。
    assert connector_service.validate_base_url(ConnectorKind.HTTP, "") == ""


# ---------------- 凭据隔离 ----------------


async def test_runtime_uses_each_tenants_own_secret(seeded_identity):
    """A 与 B 各配各的令牌，取运行时快照必须拿到各自的那一份。

    这是整层最关键的一条。闭包/缓存里串了租户，表现是「B 用了 A 的账号」，
    而调用是成功的、数据是错的——从返回值上完全看不出来。
    """
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="token-for-default")
    await _set_secret(connector_id, tenant=OTHER, secret="token-for-other")

    first = await connector_service.get_connector_runtime(
        connector_id=connector_id, tenant_id=TENANT
    )
    second = await connector_service.get_connector_runtime(
        connector_id=connector_id, tenant_id=OTHER
    )

    assert first is not None and second is not None
    assert first.secret == "token-for-default"
    assert second.secret == "token-for-other"


async def test_runtime_returns_none_for_a_tenant_without_credential(seeded_identity):
    """没配凭据的租户拿到的 runtime 里 secret 为 None，而不是别人的令牌。

    如果这里返回了别的租户的令牌，`_with_tenant_credential` 的「缺失就拒绝」
    永远不会触发——因为看起来永远都有凭据。
    """
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="only-default")

    other = await connector_service.get_connector_runtime(
        connector_id=connector_id, tenant_id=OTHER
    )

    assert other is not None
    assert other.secret is None


async def test_disabled_connector_resolves_to_none(seeded_identity):
    connector_id = await _make_connector(enabled=False)

    runtime = await connector_service.get_connector_runtime(
        connector_id=connector_id, tenant_id=TENANT
    )

    assert runtime is None


def test_runtime_repr_never_prints_the_secret():
    """frozen dataclass 的默认 repr 会把字段全打出来。

    这个对象会经 `ToolExecutionContext` 进异常 traceback 与调试日志，
    一行 repr 就够把令牌写进长期存储。
    """
    runtime = ConnectorRuntime(
        id=1,
        name="feishu",
        kind="http",
        base_url="https://api.example.com",
        auth_type="bearer",
        secret="super-secret-token",
    )

    assert "super-secret-token" not in repr(runtime)


# ---------------- 认证头 ----------------


def test_auth_headers_follow_the_configured_scheme():
    bearer = ConnectorRuntime(
        id=1, name="a", kind="http", auth_type="bearer", secret="t"
    )
    assert bearer.auth_headers() == {"Authorization": "Bearer t"}

    header = ConnectorRuntime(
        id=2,
        name="b",
        kind="http",
        auth_type="header",
        auth_header_name="X-Api-Key",
        secret="k",
    )
    assert header.auth_headers() == {"X-Api-Key": "k"}

    basic = ConnectorRuntime(
        id=3, name="c", kind="http", auth_type="basic", username="u", secret="p"
    )
    assert basic.auth_headers()["Authorization"].startswith("Basic ")

    # 没有凭据时返回空：绝不发一个空的 Authorization 头，
    # 那会被对端当成「明确告知没身份」，比不带头更难解释。
    empty = ConnectorRuntime(id=4, name="d", kind="http", auth_type="bearer")
    assert empty.auth_headers() == {}


# ---------------- 网关 ----------------


def _context(**overrides) -> ToolExecutionContext:
    base = {
        "tenant_id": TENANT,
        "user_id": "u1",
        "thread_id": "th1",
        "run_id": "r1",
        "request_id": "req1",
        "scopes": ("tools:read",),
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


async def _echo(ctx, arguments):  # noqa: ANN001
    return ToolExecutionResult(success=True, result={"ok": True})


def _tool(**overrides) -> ToolDefinition:
    base = {
        "name": "feishu_send",
        "display_name": "发消息",
        "description": "",
        "kind": "http",
        "risk_level": RiskLevel.READ,
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": _echo,
        "connector_id": None,
        "connector_name": "feishu",
        "connector_ready": True,
    }
    base.update(overrides)
    return ToolDefinition(**base)


def _gateway(*tools: ToolDefinition) -> ToolGateway:
    registry = ToolRegistry()
    gateway = ToolGateway(registry)
    for tool in tools:
        gateway.register_tool(tool)
    return gateway


async def test_missing_credential_refuses_before_calling_out(seeded_identity):
    """凭据缺失必须在**发出请求之前**拒绝，且文案要说清下一步做什么。

    静默放行的话，远端会把请求当匿名处理：可能返回空列表，模型据此
    给出「没有数据」的结论——错误静默传播，且没有任何一条日志。
    """
    connector_id = await _make_connector()

    gateway = _gateway(_tool(connector_id=connector_id))
    result = await gateway.execute_read_only(
        context=_context(),
        tool_name="feishu_send",
        arguments={},
    )

    assert result.success is False
    assert result.error_code == "CONNECTOR_CREDENTIAL_MISSING"
    assert "凭据" in (result.message or "")
    assert "控制台" in (result.message or "")


async def test_connector_endpoint_is_used_after_credential_is_set(seeded_identity):
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="tok")

    seen: dict = {}

    async def capture(ctx, arguments):  # noqa: ANN001
        seen["connector"] = ctx.connector
        return ToolExecutionResult(success=True, result={})

    gateway = _gateway(_tool(connector_id=connector_id, handler=capture))
    result = await gateway.execute_read_only(
        context=_context(), tool_name="feishu_send", arguments={}
    )

    assert result.success is True
    assert seen["connector"] is not None
    assert seen["connector"].secret == "tok"


async def test_disabled_connector_hides_its_tools(seeded_identity):
    """连接器停用 = 工具从目录消失。

    留在目录里的后果是模型挑中它、然后收到一串连接错误，
    而页面上明明显示「已停用」。
    """
    gateway = _gateway(_tool(connector_ready=False))

    visible = gateway.list_available_for_context(context=_context())

    assert [view.tool_name for view in visible] == []


# ---------------- 智能体边界 ----------------


def test_agent_boundary_grants_tools_by_connector_name():
    """`allowed_connectors` 与 `allowed_tools` 是并集。

    只按工具名授权时，接一个连接器要写二十个名字；改漏一个的表现是
    「智能体说它没有这个能力」，而界面看起来完全正常。
    """
    gateway = _gateway(_tool(name="feishu_send"), _tool(name="feishu_read"))

    visible = gateway.list_available_for_context(
        context=_context(agent_allowed_connectors=("feishu",))
    )
    assert {view.tool_name for view in visible} == {"feishu_send", "feishu_read"}

    # 配了连接器边界之后，不属于任何连接器的工具会被收窄掉。
    gateway.register_tool(_tool(name="get_platform_time", connector_name=""))
    visible = gateway.list_available_for_context(
        context=_context(agent_allowed_connectors=("feishu",))
    )
    assert "get_platform_time" not in {view.tool_name for view in visible}


def test_agent_boundary_rejects_tools_outside_connector():
    gateway = _gateway(_tool(name="feishu_send"))

    visible = gateway.list_available_for_context(
        context=_context(agent_allowed_connectors=("notion",))
    )

    assert visible == []


def test_agent_boundary_message_names_the_connector():
    """拒绝理由要点名缺哪个连接器。

    只说「未被授予工具」时，人看到的是工具名，会去工具页找——而那正是
    配置正确的地方，错在智能体边界。
    """
    tool = _tool(connector_name="feishu")

    reason = ToolGateway.agent_denial(
        tool=tool, context=_context(agent_allowed_connectors=("notion",))
    )

    assert reason is not None
    assert "feishu" in reason


# ---------------- 删除保护 ----------------


async def test_delete_is_blocked_while_tools_are_bound(seeded_identity):
    """下面还挂着工具时不让删。

    自动把工具变成「独立工具」会让它们的凭据来源切到 .env，
    可能从「能用」直接变成「用别人的令牌」——配置含义被静默改了。
    """
    from app.models.custom_tool import CustomTool

    connector_id = await _make_connector()

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            session.add(
                CustomTool(
                    name="feishu_send",
                    display_name="发消息",
                    description="",
                    method="POST",
                    url="/v1/messages",
                    connector_id=connector_id,
                )
            )
            await session.commit()

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(Connector, connector_id)
            assert row is not None
            with pytest.raises(connector_service.ConnectorConfigError):
                await connector_service.delete_connector_guard(session, connector=row)


async def test_credential_can_be_cleared(seeded_identity):
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="tok")

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(Connector, connector_id)
            assert row is not None
            assert await connector_service.clear_credential(
                session, connector=row, tenant_id=TENANT
            )
            await session.commit()

    runtime = await connector_service.get_connector_runtime(
        connector_id=connector_id, tenant_id=TENANT
    )
    assert runtime is not None
    assert runtime.secret is None

    # 再清一次返回 False，不抛：重复点击不该报错。
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(Connector, connector_id)
            assert row is not None
            assert not await connector_service.clear_credential(
                session, connector=row, tenant_id=TENANT
            )


async def test_credential_row_is_tenant_scoped(seeded_identity):
    """凭据表必须按租户隔离：这是「每个组织自己的外部账号」的落地处。

    顺带盯住一个更隐蔽的点：查询必须进 `tenant_scope`。
    `connector_credentials` 在 PostgreSQL 上有行级安全，不开上下文查出来
    永远是 0 行——而「没有凭据」与「被 RLS 挡了」在代码里长得一模一样。
    """
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="tok-default")

    async with async_session_factory() as session:
        with tenant_scope(OTHER):
            mapping = await connector_service.credential_map(session, tenant_id=OTHER)
            assert connector_id not in mapping

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            mapping = await connector_service.credential_map(session, tenant_id=TENANT)
            assert connector_id in mapping
            assert isinstance(mapping[connector_id], ConnectorCredential)


# ---------------- MCP：端点与凭据都听连接器的 ----------------


async def test_mcp_tool_uses_connector_endpoint_and_tenant_secret(
    seeded_identity, monkeypatch
):
    """接进连接器的 MCP 工具，调用时必须用**连接器的端点**与**本租户的令牌**。

    实现里最容易出错的两种写法都会让这条用例转红：
    - 继续用 `spec.server_url` / `spec.token`（那是连接器出现之前的写法，
      结果是「端点换了、令牌还是旧的」）；
    - 从闭包或模块变量里取令牌（第一个租户写进去的，第二个租户原样用）。
    """
    from app.integrations.mcp_client import McpToolDescriptor
    from app.tools import mcp_tools
    from app.tools.mcp_tools import McpToolSpec, build_dynamic_definition

    connector_id = await _make_connector(
        name=f"mcp_{uuid.uuid4().hex[:8]}",
        kind=ConnectorKind.MCP,
        base_url="https://real.example.com/mcp",
        auth_type=ConnectorAuthType.BEARER,
    )
    await _set_secret(connector_id, tenant=TENANT, secret="tenant-token")

    captured: dict = {}

    class FakeClient:
        def __init__(self, *, url, token, connect_timeout_seconds=0, call_timeout_seconds=0):  # noqa: ANN001
            captured["url"] = url
            captured["token"] = token

        async def call_tool(self, *, remote_tool_name, arguments):  # noqa: ANN001
            captured["remote"] = remote_tool_name
            return {"ok": True}

    monkeypatch.setattr(mcp_tools, "McpToolClient", FakeClient)

    spec = McpToolSpec(
        local_name="feishu_send",
        remote_name="send_message",
        display_name="发消息",
        description="",
        risk_level=RiskLevel.READ,
        # 这两个是旧写法留下的值，有连接器时必须被忽略。
        server_url="https://stale.example.com/mcp",
        token="stale-token",
        connector_id=connector_id,
        connector_name="feishu_conn",
        connector_ready=True,
    )

    definition = build_dynamic_definition(
        spec,
        descriptor=McpToolDescriptor(
            name="send_message",
            description="",
            input_schema={"type": "object", "properties": {}},
        ),
    )

    gateway = _gateway(definition)
    result = await gateway.execute_read_only(
        context=_context(), tool_name="feishu_send", arguments={}
    )

    assert result.success is True, result.message
    assert captured["url"] == "https://real.example.com/mcp"
    assert captured["token"] == "tenant-token"
    assert captured["remote"] == "send_message"


# ---------------- 批量接入：默认风险等级取严的一侧 ----------------


async def _bind(monkeypatch, connector_id: int, items: list[dict]) -> list[str]:
    """跑一次真实绑定流程，远端用假 descriptor 顶掉网络。"""
    from app.integrations.mcp_client import McpToolDescriptor

    remote = {
        "send_message": McpToolDescriptor(name="send_message", description="", input_schema={}),
        "delete_message": McpToolDescriptor(name="delete_message", description="", input_schema={}),
    }

    async def fake_fetch(*, url, token):  # noqa: ANN001
        return remote

    monkeypatch.setattr(connector_service, "fetch_remote_tools", fake_fetch)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            connector = await session.get(Connector, connector_id)
            assert connector is not None
            return await connector_service.bind_mcp_tools(
                session,
                _gateway(),
                connector=connector,
                secret=None,
                items=items,
                actor_id="tester",
                tenant_id=TENANT,
            )


async def test_batch_bind_defaults_to_write_risk(seeded_identity, monkeypatch):
    """批量接入不传风险等级时，默认「写入」，不是「读取」。

    读默认值的代价是不对等的：默认「读取」意味着远端一个 `delete_message`
    接进来就是**免审批直接执行**，而它的名字和别的工具长得一样；
    默认「写入」只是多点一次确认。一次接二十个工具时，没人会逐个判断风险，
    所以默认值必须自己站在安全的一侧。
    """
    from app.models.mcp_registered import McpRegisteredTool

    connector_id = await _make_connector(
        name=f"mcp_{uuid.uuid4().hex[:8]}",
        kind=ConnectorKind.MCP,
        base_url="https://mcp.example.com/mcp",
    )
    local = f"t_{uuid.uuid4().hex[:8]}"
    await _bind(monkeypatch, connector_id, [{"remote_name": "delete_message", "local_name": local}])

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.scalar(
                select(McpRegisteredTool).where(McpRegisteredTool.local_name == local)
            )
            assert row is not None
            assert row.risk_level == RiskLevel.WRITE


async def test_batch_bind_rejects_unknown_risk_level(seeded_identity, monkeypatch):
    """非法风险等级要是 400 而不是 500。

    `RiskLevel("banana")` 抛的是 ValueError，直接漏出去会变成 500——
    让人以为是服务端故障，反复重试同一份错误请求。
    """
    connector_id = await _make_connector(
        name=f"mcp_{uuid.uuid4().hex[:8]}",
        kind=ConnectorKind.MCP,
        base_url="https://mcp.example.com/mcp",
    )

    with pytest.raises(connector_service.ConnectorConfigError) as exc:
        await _bind(
            monkeypatch,
            connector_id,
            [
                {
                    "remote_name": "send_message",
                    "local_name": f"t_{uuid.uuid4().hex[:8]}",
                    "risk_level": "banana",
                }
            ],
        )

    assert "banana" in str(exc.value)


# ---------------- 删除连接器：凭据不能留在库里 ----------------


async def test_delete_purges_current_tenant_credential(seeded_identity):
    """删掉连接器后，**本租户**的凭据要当场消失。

    `connector_credentials.connector_id` 没有外键（外键在 SQLite 上是空操作，
    所以测试全绿也说明不了生产行为），清理只能靠这一步显式删除。
    漏了它，密文会留在库里，而连接器已经不存在——事后没人说得清那串东西
    属于哪个系统，也没人敢删。
    """
    connector_id = await _make_connector()
    await _set_secret(connector_id, tenant=TENANT, secret="secret-a")
    await _set_secret(connector_id, tenant=OTHER, secret="secret-b")

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(Connector, connector_id)
            assert row is not None
            await session.delete(row)
            await session.commit()

    with tenant_scope(TENANT):
        assert await connector_service.purge_credentials(connector_id=connector_id) == 1

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            assert (
                await session.scalar(
                    select(ConnectorCredential).where(
                        ConnectorCredential.connector_id == connector_id
                    )
                )
                is None
            )


async def test_purge_needs_tenant_context(seeded_identity):
    """没有租户上下文时直接报错。

    表达式上它只会删「当前租户」的行；没有上下文就无从删起。
    静默返回 0 会让「清理了但没删到」看起来像「本来就没有」。
    """
    with pytest.raises(RuntimeError):
        await connector_service.purge_credentials(connector_id=0)


async def test_maintenance_purges_orphan_credentials_per_tenant(seeded_identity):
    """剩余租户的孤儿凭据由维护任务逐租户扫掉。

    跨租户清理在请求路径上走不通（RLS + `tenant_scope` 禁止切换），
    所以删除接口只管本租户，其余靠这一条。这里验证两件事：
    孤儿行会被删，**还活着的连接器的凭据一行都不能动**。
    """
    live_id = await _make_connector()
    dead_id = await _make_connector()

    for connector_id in (live_id, dead_id):
        await _set_secret(connector_id, tenant=TENANT, secret=f"s-{connector_id}")
        await _set_secret(connector_id, tenant=OTHER, secret=f"s-{connector_id}")

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await session.get(Connector, dead_id)
            assert row is not None
            await session.delete(row)
            await session.commit()

    # 维护任务对每个租户各跑一次。
    for tenant in (TENANT, OTHER):
        with tenant_scope(tenant):
            await connector_service.purge_orphan_credentials()

    for tenant in (TENANT, OTHER):
        async with async_session_factory() as session:
            with tenant_scope(tenant):
                rows = (
                    await session.scalars(
                        select(ConnectorCredential).where(
                            ConnectorCredential.connector_id.in_([live_id, dead_id])
                        )
                    )
                ).all()

        assert [row.connector_id for row in rows] == [live_id], tenant
