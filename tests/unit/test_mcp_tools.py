# tests/unit/test_mcp_tools.py
"""MCP 接入的单元测试。

不连真实 MCP Server：这里验证的是「本地这一层」的行为——
白名单映射、业务成败映射、凭据缺失、控制下发的两段串联。
协议层是否正确由部署后的联调验证（见 docs/MCP_INTEGRATION.md）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.integrations.mcp_client import (
    McpAuthError,
    McpToolClient,
    McpToolDescriptor,
    McpUnavailableError,
)
from app.models.approval import RiskLevel
from app.tools import mcp_tools
from app.tools.gateway import ToolGateway
from app.tools.mcp_tools import (
    _REMOTE_BY_LOCAL,
    APPLY_TOOL_NAME,
    MCP_TOOL_SPECS,
    McpCredentialMissingError,
    McpToolSpec,
    _make_apply_handler,
    _make_passthrough_handler,
    _to_result,
    register_mcp_tools,
)
from app.tools.registry import ToolRegistry
from app.tools.types import ToolExecutionContext


def _context(tenant_id: str = "default") -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=tenant_id,
        user_id="u1",
        thread_id="t1",
        run_id="r1",
        request_id="req1",
        roles=("dev",),
        scopes=("tools:read",),
    )


class FakeClient:
    """记录调用的假客户端。"""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        *,
        remote_tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((remote_tool_name, dict(arguments)))
        return self.responses[remote_tool_name]


@pytest.fixture
def mcp_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "token-default"})


# ---------------- 白名单 ----------------


def test_every_spec_maps_to_a_remote_tool() -> None:
    """每个本地工具都必须能追到远端工具名，否则注册时会静默消失。"""
    for spec in MCP_TOOL_SPECS:
        if spec.local_name == APPLY_TOOL_NAME:
            assert spec.local_name not in _REMOTE_BY_LOCAL
            continue
        assert spec.local_name in _REMOTE_BY_LOCAL, spec.local_name


def test_local_names_are_unique_and_prefixed() -> None:
    names = [spec.local_name for spec in MCP_TOOL_SPECS]
    assert len(names) == len(set(names))

    # 统一前缀，避免与本地工具重名时互相覆盖。
    assert all(name.startswith("prv_") for name in names)


_WRITE_RISKS = {
    RiskLevel.WRITE,
    RiskLevel.EXECUTE,
    RiskLevel.DESTRUCTIVE,
    RiskLevel.EXTERNAL,
}


def test_write_risk_tools_require_approval() -> None:
    """
    会产生外部副作用的工具必须走审批。

    Gateway 对 write 及以上风险本来就会强制审批，这里再断言一次，
    是为了防止有人把 execute 类工具的 risk 误改成 read。
    """
    for spec in MCP_TOOL_SPECS:
        if spec.risk_level in _WRITE_RISKS:
            assert spec.requires_approval, spec.local_name


def test_export_tools_do_not_require_approval() -> None:
    """
    导出类工具（趋势/CSV/PDF）只读站点数据，副作用仅是在 MCP 服务端落一个文件，
    不改变站点状态。用户查趋势不该等审批，所以定成 normal。

    这条断言的作用是防回退：如果有人把它们改回 write，或网关以后把 normal
    也算作需审批，这里会先红。
    """
    exports = {
        "prv_pressure_trend",
        "prv_export_records",
        "prv_download_report_pdf",
    }
    specs = {spec.local_name: spec for spec in MCP_TOOL_SPECS}

    for name in exports:
        spec = specs[name]
        assert spec.risk_level is RiskLevel.NORMAL, name
        assert spec.requires_approval is False, name


def test_approval_set_is_only_the_control_dispatch() -> None:
    """当前只有真实下发需要审批；多出一个都说明风险分级被改歪了。"""
    requiring = {spec.local_name for spec in MCP_TOOL_SPECS if spec.requires_approval}

    assert requiring == {"prv_apply_station_control"}


# ---------------- 结果映射 ----------------


def test_api_payload_is_marked_verified() -> None:
    result = _to_result({"success": True, "data_source": "API", "count": 1})

    assert result.success is True
    assert result.verified is True
    assert result.result["count"] == 1


def test_demo_payload_is_not_verified() -> None:
    result = _to_result({"success": True, "data_source": "mock"})

    assert result.success is True
    assert result.verified is False
    assert "演示数据" in (result.message or "")


def test_business_failure_is_not_success() -> None:
    """
    该 MCP 服务的协议层 is_error 恒为 False，业务失败只在 payload 里体现。
    把这种结果当成功，等于对用户谎称操作已完成。
    """
    result = _to_result({"success": False, "error": "未指定 station"})

    assert result.success is False
    assert result.verified is False
    assert result.error_code == "MCP_TOOL_ERROR"
    assert "未指定 station" in (result.message or "")


def test_missing_success_field_is_treated_as_success() -> None:
    """getServerStatus 这类工具不返回 success 字段，不能因此判失败。"""
    result = _to_result({"deploy_mode": "http"})

    assert result.success is True
    assert result.verified is False


def test_success_message_replaces_generic_prefix() -> None:
    """导出类工具要能说清「取到了什么」，而不是统一回一句「已完成」。"""
    result = _to_result({"success": True, "data_source": "API"}, ok_message="趋势曲线已取回。")

    assert result.message.startswith("趋势曲线已取回。")


def test_success_message_is_not_used_on_failure() -> None:
    """
    成功文案只在成功时说。

    失败时套上「趋势曲线已取回」，用户会以为拿到了数据、只是附带一句报错。
    """
    result = _to_result({"success": False, "error": "start 格式不对"}, ok_message="趋势曲线已取回。")

    assert "趋势曲线已取回" not in (result.message or "")
    assert "start 格式不对" in (result.message or "")


def test_empty_station_list_explains_permission_boundary() -> None:
    """
    空白名单令牌的实测行为是 success=true + count=0，不是报错。

    只回一句「已完成」的话，用户会把权限边界读成「站点全离线」或
    「服务挂了」，所以这里必须把原因写进 message。
    """
    result = _to_result(
        {
            "success": True,
            "data_source": "API",
            "count": 0,
            "total_authorized": 10,
            "visible_to_identity": 0,
            "identity_station_filter": [],
            "stations": [],
        },
        tool_name="prv_list_stations",
    )

    assert result.success is True

    message = result.message or ""

    assert "空白名单" in message
    assert "10" in message
    assert "服务故障" in message


def test_empty_result_is_not_reported_as_plain_success() -> None:
    result = _to_result(
        {"success": True, "data_source": "API", "count": 0, "records": []},
        tool_name="prv_query_alarms",
    )

    assert result.success is True
    assert "未返回任何记录" in (result.message or "")


def test_non_empty_result_keeps_neutral_message() -> None:
    result = _to_result(
        {"success": True, "data_source": "API", "count": 2, "stations": [{}, {}]},
        tool_name="prv_list_stations",
    )

    assert result.message == "已完成远端调用。"


def test_apply_prefix_wins_over_empty_detection() -> None:
    """控制下发的 message 由 handler 决定，不能被空结果推断覆盖。"""
    result = _to_result(
        {"success": True, "data_source": "API", "count": 0},
        message_prefix="控制指令已提交。",
        tool_name=APPLY_TOOL_NAME,
    )

    assert result.message == "控制指令已提交。"


# ---------------- 凭据 ----------------


async def test_missing_tenant_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {})

    spec = MCP_TOOL_SPECS[0]
    handler = _make_passthrough_handler(spec, "getServerStatus")

    result = await handler(_context(), {})

    assert result.success is False
    assert result.error_code == "MCP_CREDENTIAL_MISSING"


async def test_other_tenant_token_is_not_reused(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    只给 default 配了令牌时，test 租户必须被拒绝。

    回落用别人的令牌是最难事后发现的越权，所以这里是硬失败。
    """
    monkeypatch.setattr(
        mcp_tools,
        "_client_for",
        lambda context, *, call_timeout, spec=None: (_ for _ in ()).throw(
            McpCredentialMissingError("租户 test 未配置 MCP 访问令牌。")
        ),
    )

    spec = MCP_TOOL_SPECS[0]
    handler = _make_passthrough_handler(spec, "getServerStatus")

    result = await handler(_context("test"), {})

    assert result.success is False
    assert result.error_code == "MCP_CREDENTIAL_MISSING"


async def test_auth_rejection_is_not_retryable(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据被拒不能标成可重试：重发只会得到同一个结果，还会掩盖真正的问题。"""

    def _raise(context, *, call_timeout, spec=None):  # noqa: ANN001, ARG001
        raise McpAuthError("令牌无效或已停用。")

    monkeypatch.setattr(mcp_tools, "_client_for", _raise)

    spec = MCP_TOOL_SPECS[0]
    handler = _make_passthrough_handler(spec, "getServerStatus")

    result = await handler(_context(), {})

    assert result.success is False
    assert result.error_code == "MCP_AUTH_REJECTED"
    assert result.retryable is False


async def test_transport_error_is_retryable(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(context, *, call_timeout, spec=None):  # noqa: ANN001, ARG001
        raise McpUnavailableError("MCP 服务不可达：ConnectError。")

    monkeypatch.setattr(mcp_tools, "_client_for", _raise)

    spec = MCP_TOOL_SPECS[0]
    handler = _make_passthrough_handler(spec, "getServerStatus")

    result = await handler(_context(), {})

    assert result.success is False
    assert result.error_code == "MCP_UNAVAILABLE"
    assert result.retryable is True


# ---------------- 控制下发 ----------------


async def test_apply_runs_prepare_then_execute(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient(
        {
            "prepareStationControl": {
                "success": True,
                "confirmation_can_execute": True,
                "preview_id": "a" * 32,
                "preview": {"action": "set_press_level"},
            },
            "executeStationControl": {
                "success": True,
                "data_source": "API",
                "command_id": "cmd-1",
            },
        }
    )
    monkeypatch.setattr(
        mcp_tools,
        "_client_for",
        lambda context, *, call_timeout, spec=None: fake,
    )

    spec = next(s for s in MCP_TOOL_SPECS if s.local_name == APPLY_TOOL_NAME)
    result = await _make_apply_handler(spec)(
        _context(), {"station": "T002509008", "action": "set_press_level"}
    )

    assert [name for name, _ in fake.calls] == [
        "prepareStationControl",
        "executeStationControl",
    ]
    execute_args = fake.calls[1][1]
    assert execute_args["preview_id"] == "a" * 32
    assert execute_args["confirm"] is True

    assert result.success is True
    assert result.external_operation_id == "cmd-1"


async def test_apply_stops_when_prepare_fails(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预演不通过时绝不能再调 execute —— 那等于绕过条件检查直接写设备。"""
    fake = FakeClient(
        {
            "prepareStationControl": {
                "success": False,
                "error": "当前令牌 write_scope=none",
            }
        }
    )
    monkeypatch.setattr(
        mcp_tools,
        "_client_for",
        lambda context, *, call_timeout, spec=None: fake,
    )

    spec = next(s for s in MCP_TOOL_SPECS if s.local_name == APPLY_TOOL_NAME)
    result = await _make_apply_handler(spec)(
        _context(), {"station": "T002509008", "action": "set_press_level"}
    )

    assert [name for name, _ in fake.calls] == ["prepareStationControl"]
    assert result.success is False
    assert "未下发任何指令" in (result.message or "")


async def test_apply_stops_when_precondition_not_met(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    prepare 只校验参数，能不能执行看 confirmation_can_execute。

    写开关关闭、动作不在白名单时 prepare 依然 success=true，此时继续调 execute
    等于把「审批通过」当成「条件满足」。
    """
    fake = FakeClient(
        {
            "prepareStationControl": {
                "success": True,
                "confirmation_can_execute": False,
                "blockers": ["服务未开启写入（write_enabled=false）"],
                "preview_id": "b" * 32,
            }
        }
    )
    monkeypatch.setattr(
        mcp_tools,
        "_client_for",
        lambda context, *, call_timeout, spec=None: fake,
    )

    spec = next(s for s in MCP_TOOL_SPECS if s.local_name == APPLY_TOOL_NAME)
    result = await _make_apply_handler(spec)(
        _context(), {"station": "T002509008", "action": "high_pressure"}
    )

    assert [name for name, _ in fake.calls] == ["prepareStationControl"]
    assert result.success is False
    assert result.error_code == "MCP_PRECONDITION_FAILED"
    assert "write_enabled" in (result.message or "")


def test_schema_hints_explain_control_parameters() -> None:
    """
    远端 schema 只有类型没有语义，模型会把 action 填成自然语言。
    本地必须补上取值说明，并在控制类工具上写清约束。
    """
    spec = next(s for s in MCP_TOOL_SPECS if s.local_name == APPLY_TOOL_NAME)
    schema = mcp_tools._apply_schema_hints(
        {
            "type": "object",
            "properties": {
                "station": {"type": "string"},
                "action": {"type": "string"},
                "weekday": {"type": "integer"},
                "schedule": {"type": "array"},
            },
        },
        local_name=spec.local_name,
    )

    assert "close_main" in schema["properties"]["action"]["description"]
    assert "序列号" in schema["properties"]["station"]["description"]
    assert schema["additionalProperties"] is False


def test_schema_hints_do_not_mutate_remote_schema() -> None:
    """远端 schema 对象是共享的，打补丁不能改到原对象。"""
    original = {"type": "object", "properties": {"station": {"type": "string"}}}
    mcp_tools._apply_schema_hints(original, local_name="prv_query_pressure")

    assert "description" not in original["properties"]["station"]


# ---------------- 注册 ----------------


class FakeProbe:
    """按白名单返回描述的假客户端，用于验证注册逻辑。"""

    descriptors: list[McpToolDescriptor] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def list_tools(self) -> list[McpToolDescriptor]:
        return list(self.descriptors)


async def test_register_skips_tools_missing_on_remote(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    远端少了一个工具时只跳过它，不能让注册整体失败，
    更不能把「远端没有」当成「本地可以凭空注册」。
    """
    FakeProbe.descriptors = [
        McpToolDescriptor(
            name="getServerStatus",
            description="",
            input_schema={"type": "object", "properties": {}},
        )
    ]
    monkeypatch.setattr(mcp_tools, "McpToolClient", FakeProbe)
    monkeypatch.setattr(settings, "mcp_enabled", True)

    gateway = ToolGateway(ToolRegistry())

    registered = await register_mcp_tools(gateway)

    assert registered == 1
    assert [tool.name for tool in gateway.list_tools()] == ["prv_server_status"]


async def test_register_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_enabled", False)

    gateway = ToolGateway(ToolRegistry())

    assert await register_mcp_tools(gateway) == 0
    assert gateway.list_tools() == []


async def test_register_tolerates_unreachable_server(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenProbe(FakeProbe):
        async def list_tools(self) -> list[McpToolDescriptor]:
            raise McpUnavailableError("MCP 服务不可达：ConnectError。")

    monkeypatch.setattr(mcp_tools, "McpToolClient", BrokenProbe)
    monkeypatch.setattr(settings, "mcp_enabled", True)

    gateway = ToolGateway(ToolRegistry())

    assert await register_mcp_tools(gateway) == 0
    assert gateway.list_tools() == []


def test_schema_hints_cover_only_declared_tools() -> None:
    """说明表里的本地名必须真实存在，否则整段提示静默失效。"""
    declared = {spec.local_name for spec in MCP_TOOL_SPECS}

    unknown = set(mcp_tools._SCHEMA_HINTS) - declared

    assert not unknown, f"这些说明找不到对应工具：{sorted(unknown)}"


def test_unknown_param_hint_is_ignored_not_injected() -> None:
    """远端没有的参数不能被凭空塞进 schema——那会让模型以为可以传。"""
    schema = {"type": "object", "properties": {"station": {"type": "string"}}}

    out = mcp_tools._apply_schema_hints(schema, local_name="prv_download_report_pdf")

    assert "report_path" not in out["properties"]
    assert out["properties"]["station"]["description"]


def test_download_report_explains_report_path_source() -> None:
    """report_path 只能照抄上游返回值，模型自己拼路径一定失败。"""
    schema = {
        "type": "object",
        "properties": {"report_path": {"type": "string"}, "file_name": {"type": "string"}},
    }

    out = mcp_tools._apply_schema_hints(schema, local_name="prv_download_report_pdf")

    hint = out["properties"]["report_path"]["description"]
    assert "原样" in hint and "prv_relief_report" in hint


async def test_register_includes_admin_attached_tools(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制台接入的工具要进注册表，入参 schema 来自远端而不是本地编造。"""
    FakeProbe.descriptors = [
        McpToolDescriptor(
            name="calendarLookup",
            description="查询日程",
            input_schema={
                "type": "object",
                "properties": {"date": {"type": "string"}},
            },
        )
    ]
    monkeypatch.setattr(mcp_tools, "McpToolClient", FakeProbe)
    monkeypatch.setattr(settings, "mcp_enabled", True)

    gateway = ToolGateway(ToolRegistry())
    spec = McpToolSpec(
        local_name="calendar_lookup",
        display_name="查询日程",
        description="按日期查日程。",
        risk_level=RiskLevel.READ,
        remote_name="calendarLookup",
        timeout_seconds=30,
    )

    registered = await register_mcp_tools(gateway, extra_specs=(spec,))

    assert registered == 1
    tool = gateway.get_tool("calendar_lookup")
    assert tool.source == "mcp"
    assert tool.timeout_seconds == 30
    assert "date" in tool.input_schema["properties"]


async def test_register_lets_code_specs_win_over_admin_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同名时以代码为准：配置不该有能力顶掉审批路径上的实现。"""
    monkeypatch.setattr(
        mcp_tools,
        "McpToolClient",
        type(
            "Probe",
            (),
            {
                "__init__": lambda self, **kwargs: None,
                "list_tools": lambda self: _fake_descriptors(),
            },
        ),
    )
    monkeypatch.setattr(settings, "mcp_enabled", True)
    monkeypatch.setattr(settings, "mcp_server_url", "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "tok"})

    gateway = ToolGateway(ToolRegistry())
    shadow = McpToolSpec(
        local_name="prv_query_pressure",
        display_name="被顶掉的配置",
        description="不该生效。",
        risk_level=RiskLevel.DESTRUCTIVE,
        remote_name="queryPressure",
        requires_approval=False,
    )

    await register_mcp_tools(gateway, extra_specs=(shadow,))

    tool = gateway.get_tool("prv_query_pressure")
    assert tool.display_name != "被顶掉的配置"
    # 代码里的读取工具不需要审批；若配置生效会变成危险工具。
    assert tool.risk_level is RiskLevel.READ


async def _fake_descriptors() -> list[McpToolDescriptor]:
    return [
        McpToolDescriptor(
            name="queryPressure",
            description="",
            input_schema={"type": "object", "properties": {}},
        )
    ]


# ---------------- 多端点 ----------------


async def test_register_fetches_each_endpoint_it_needs(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接入项自带端点时，注册要去那个端点取 schema，而不是默认端点。

    远端名可能只在那个服务上存在：拿默认端点去查，注册会静默跳过它，
    表现为「接了但模型看不到」。
    """
    second = "http://127.0.0.1:8001/mcp"

    class ByUrlProbe(FakeProbe):
        async def list_tools(self) -> list[McpToolDescriptor]:
            if self.kwargs.get("url") == second:
                return [
                    McpToolDescriptor(
                        name="ocr_file",
                        description="识别文件文字",
                        input_schema={
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                        },
                    )
                ]
            return []

    monkeypatch.setattr(mcp_tools, "McpToolClient", ByUrlProbe)
    monkeypatch.setattr(settings, "mcp_enabled", True)

    spec = McpToolSpec(
        local_name="ocr_file",
        display_name="识别文件文字",
        description="识别服务器上的图片或 PDF。",
        risk_level=RiskLevel.READ,
        remote_name="ocr_file",
        server_url=second,
    )

    gateway = ToolGateway(ToolRegistry())

    assert await register_mcp_tools(gateway, extra_specs=(spec,)) == 1
    assert "file_path" in gateway.get_tool("ocr_file").input_schema["properties"]


async def test_foreign_endpoint_does_not_reuse_default_tenant_token(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部端点没登记令牌就不要发 Authorization：那是把凭据送给另一个服务。"""
    second = "http://127.0.0.1:8001/mcp"
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "prv-secret"})

    foreign = McpToolSpec(
        local_name="ocr_file",
        display_name="识别文件文字",
        description="",
        risk_level=RiskLevel.READ,
        remote_name="ocr_file",
        server_url=second,
    )

    client = mcp_tools._client_for(_context(), call_timeout=30, spec=foreign)
    assert client._token is None

    # 外部端点自己登记了令牌就用它
    with_token = McpToolSpec(
        local_name="other_tool",
        display_name="另一个",
        description="",
        risk_level=RiskLevel.READ,
        remote_name="other",
        server_url=second,
        token="foreign-secret",
    )
    assert mcp_tools._client_for(_context(), call_timeout=30, spec=with_token)._token == (
        "foreign-secret"
    )


async def test_default_endpoint_still_uses_tenant_token(
    mcp_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认端点保持原行为：按租户取 .env 里的令牌。"""
    monkeypatch.setattr(settings, "mcp_tenant_tokens", {"default": "prv-secret"})

    spec = McpToolSpec(
        local_name="some_tool",
        display_name="某工具",
        description="",
        risk_level=RiskLevel.READ,
        remote_name="someTool",
        server_url=settings.mcp_server_url,
    )

    client = mcp_tools._client_for(_context(), call_timeout=30, spec=spec)
    assert client._token == "prv-secret"


def test_client_accepts_no_token_but_rejects_empty_string() -> None:
    """None = 该端点不鉴权；空串 = 配置读空了，仍是错误。"""
    assert McpToolClient(url="http://127.0.0.1:8001/mcp", token=None)._token is None

    with pytest.raises(McpAuthError):
        McpToolClient(url="http://127.0.0.1:8001/mcp", token="")
