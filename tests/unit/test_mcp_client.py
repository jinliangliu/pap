# tests/unit/test_mcp_client.py
"""MCP 客户端的错误分类测试。

不连真实服务：这里盯的是「同一种失败会不会被说成另一种失败」。
依据是 prv MCP Server 独立部署后的实际行为——鉴权失败由 HTTP 层前筛
返回 401 + JSON-RPC 错误体（code -32001 / -32002），协议版本不合法返回 400。
分类错的代价是排障方向跑偏：把「令牌无效」报成「协议错误」，人会去查网络和 URL。
"""

from __future__ import annotations

import httpx2
import pytest
from mcp.shared.exceptions import MCPError as SdkMcpError

from app.integrations.mcp_client import (
    McpAuthError,
    McpProtocolError,
    McpTimeoutError,
    McpToolClient,
    McpUnavailableError,
)

_BASE = "http://127.0.0.1:8765/mcp"


def _client() -> McpToolClient:
    return McpToolClient(url=_BASE, token="t0ken")


def _classify(exc: BaseException):
    """分类逻辑与网络无关，构造一个客户端实例直接喂异常即可。"""
    return _client()._classify(exc)


def _status_error(status: int, body: dict | None = None) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", "http://127.0.0.1:8765/mcp")
    response = httpx2.Response(status, request=request, json=body) if body else httpx2.Response(
        status, request=request
    )
    return httpx2.HTTPStatusError(f"{status}", request=request, response=response)


# ---------------- 构造阶段的守卫 ----------------


def test_empty_token_is_rejected_before_any_request() -> None:
    """空令牌不能发出去：httpx 会报 Illegal header value，看起来像协议问题。"""
    with pytest.raises(McpAuthError) as excinfo:
        McpToolClient(url="http://127.0.0.1:8765/mcp", token="  ")

    assert "令牌" in str(excinfo.value)


def test_non_http_url_is_rejected() -> None:
    with pytest.raises(McpUnavailableError):
        McpToolClient(url="127.0.0.1:8765/mcp", token="t0ken")


# ---------------- 服务端 JSON-RPC 错误 ----------------


def test_missing_token_maps_to_auth_error() -> None:
    out = _classify(SdkMcpError(code=-32001, message="缺少 Bearer 令牌。"))

    assert isinstance(out, McpAuthError)
    assert out.code == "MCP_AUTH_REJECTED"


def test_invalid_token_maps_to_auth_error_and_keeps_server_message() -> None:
    out = _classify(SdkMcpError(code=-32002, message="令牌无效或已停用。"))

    assert isinstance(out, McpAuthError)
    # 服务端文案要透出来，别换成「协议错误」这类让人查错方向的措辞。
    assert "令牌无效或已停用" in str(out)


def test_unsupported_protocol_version_is_actionable() -> None:
    out = _classify(SdkMcpError(code=-32600, message="Unsupported MCP-Protocol-Version"))

    assert isinstance(out, McpProtocolError)
    assert "协议版本" in str(out)
    assert "SDK" in str(out)


def test_generic_error_response_hints_at_gateway() -> None:
    """SDK 在非 2xx 且 body 不是 JSON-RPC 时自己编的文案。"""
    out = _classify(SdkMcpError(code=-32603, message="Server returned an error response"))

    assert isinstance(out, McpProtocolError)
    assert "网关" in str(out)


def _classify_after(status: int):
    """分类前先塞入传输层记下的状态码，模拟「SDK 把响应丢了」的真实路径。"""
    client = _client()
    client._last_status = status
    return client._classify(
        SdkMcpError(code=-32603, message="Server returned an error response")
    )


def test_generic_message_with_401_is_auth_not_gateway() -> None:
    """同样是那句兜底文案，401 必须是凭据问题。

    线上踩过：连接器凭据被清空后点「发现工具」，远端回 401（body 是
    `{"error": "invalid_token"}`，不是 JSON-RPC），SDK 把状态码丢了，
    平台照着「网关/反向代理拦下了请求」报错。人于是去查隧道，而真正要做的是
    把凭据填回去。这条断言盯的就是这个方向性错误。
    """
    out = _classify_after(401)

    assert isinstance(out, McpAuthError)
    assert "401" in str(out)
    assert "凭据" in str(out)
    assert "网关" not in str(out)


def test_generic_message_with_502_points_at_the_gateway() -> None:
    out = _classify_after(502)

    assert isinstance(out, McpUnavailableError)
    assert "502" in str(out)
    assert "网关" in str(out)


def test_generic_message_with_404_points_at_the_path() -> None:
    out = _classify_after(404)

    assert isinstance(out, McpUnavailableError)
    assert "/mcp" in str(out)


def test_terminated_session_is_retryable() -> None:
    out = _classify(SdkMcpError(code=-32600, message="Session terminated"))

    assert isinstance(out, McpUnavailableError)


def test_unknown_rpc_error_keeps_code_and_message() -> None:
    out = _classify(SdkMcpError(code=-32050, message="站点不存在"))

    assert isinstance(out, McpProtocolError)
    assert "-32050" in str(out) and "站点不存在" in str(out)


# ---------------- HTTP 层错误 ----------------


def test_http_401_without_body_maps_to_auth_error() -> None:
    out = _classify(_status_error(401))

    assert isinstance(out, McpAuthError)


def test_http_401_body_code_wins_over_status() -> None:
    """401 + JSON-RPC 体时要按 code 细分，而不是都塌成一句 401。"""
    out = _classify(
        _status_error(
            401,
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32002, "message": "令牌无效或已停用。"}},
        )
    )

    assert isinstance(out, McpAuthError)
    assert "令牌无效" in str(out)


def test_http_400_protocol_version_body_is_parsed() -> None:
    out = _classify(
        _status_error(
            400,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Unsupported MCP-Protocol-Version"},
            },
        )
    )

    assert isinstance(out, McpProtocolError)
    assert "协议版本" in str(out)


def test_http_404_points_at_url() -> None:
    out = _classify(_status_error(404))

    assert isinstance(out, McpUnavailableError)
    assert "URL" in str(out)


def test_http_500_is_protocol_error_not_auth() -> None:
    out = _classify(_status_error(500))

    assert isinstance(out, McpProtocolError)
    assert "500" in str(out)


# ---------------- 传输层 ----------------


def test_timeout_maps_to_timeout() -> None:
    assert isinstance(_classify(TimeoutError()), McpTimeoutError)


def test_connect_error_maps_to_unavailable() -> None:
    assert isinstance(_classify(httpx2.ConnectError("refused")), McpUnavailableError)


def test_exception_group_is_unwrapped_to_root_cause() -> None:
    """TaskGroup 会把真实异常埋起来，只留一句 unhandled errors。"""
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ValueError("noise"), SdkMcpError(code=-32002, message="令牌无效或已停用。")],
    )

    out = _classify(group)

    # 不能只看第一个子异常：排在前面的是噪音，真正的根因在第二个。
    assert isinstance(out, McpAuthError)
    assert "令牌无效" in str(out)


def test_unknown_exception_is_protocol_error() -> None:
    out = _classify(RuntimeError("boom"))

    assert isinstance(out, McpProtocolError)
    assert "boom" in str(out)
