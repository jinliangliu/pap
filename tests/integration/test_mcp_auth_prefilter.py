# tests/integration/test_mcp_auth_prefilter.py
"""对着「新版 MCP Server 的鉴权前筛」跑一次真实链路。

不 mock 客户端内部：起一个真的 HTTP 服务，复刻 prv MCP Server 在
`PRV_STRICT_HTTP_AUTH=1` 下的行为——任意方法的 `/mcp` 无有效令牌时返回
`401 + WWW-Authenticate`，响应体仍是 JSON-RPC 错误（code -32001/-32002），
协议版本非法时返回 `400`。

为什么值得单独起服务测：客户端拿到的不是 HTTP 状态，而是 SDK 从响应体解析出的
JSON-RPC 错误。这条路径只有在真跑时才暴露（SDK 在非 2xx + JSON 体时会优先用
body 里的 error，而不是状态码）。
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.integrations.mcp_client import (
    McpAuthError,
    McpProtocolError,
    McpToolClient,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _jsonrpc_error(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


def _build_strict_server() -> Starlette:
    """复刻前筛：只做鉴权与版本判断，不做任何 MCP 业务。"""

    async def mcp_endpoint(request: Request) -> JSONResponse:
        version = request.headers.get("mcp-protocol-version")
        # 服务端先校验版本再鉴权，避免把 400 掩盖成 401
        if version and version not in ("2025-06-18", "2025-11-25"):
            return JSONResponse(
                _jsonrpc_error(-32600, "Unsupported MCP-Protocol-Version"), status_code=400
            )

        auth = request.headers.get("authorization", "")
        token = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""

        if not token:
            return JSONResponse(
                _jsonrpc_error(-32001, "缺少 Bearer 令牌。"),
                status_code=401,
                headers={"www-authenticate": 'Bearer realm="station-mcp", error="invalid_request"'},
            )

        if token != "good-token":
            return JSONResponse(
                _jsonrpc_error(-32002, "令牌无效或已停用。"),
                status_code=401,
                headers={"www-authenticate": 'Bearer realm="station-mcp", error="invalid_token"'},
            )

        # 合法令牌走到这里：本项目不实现 MCP 协议，只用来证明鉴权链路已通过。
        return JSONResponse({"ok": True}, status_code=200)

    return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST", "GET", "DELETE"])])


@asynccontextmanager
async def _serve() -> AsyncIterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        _build_strict_server(),
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - 起不来就是环境问题
        server.should_exit = True
        await task
        raise RuntimeError("测试服务未能启动")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def test_invalid_token_surfaces_as_auth_rejection() -> None:
    async with _serve() as url:
        client = McpToolClient(url=url, token="bad-token", connect_timeout_seconds=5)
        with pytest.raises(McpAuthError) as excinfo:
            await client.list_tools()

    # 服务端的可读文案要透出来，别退化成「协议错误」
    assert "令牌无效或已停用" in str(excinfo.value)
    assert excinfo.value.code == "MCP_AUTH_REJECTED"


async def test_unauthorized_status_is_not_reported_as_protocol_error() -> None:
    """401 + JSON-RPC 体：分类必须落在鉴权上，而不是协议错。"""
    async with _serve() as url:
        client = McpToolClient(url=url, token="bad-token", connect_timeout_seconds=5)
        try:
            await client.list_tools()
        except Exception as exc:  # noqa: BLE001 - 断言分类本身
            assert isinstance(exc, McpAuthError), f"实际类型 {type(exc).__name__}: {exc}"
        else:  # pragma: no cover - 服务端一定拒绝
            pytest.fail("预期鉴权失败")


async def test_unsupported_protocol_version_is_explained() -> None:
    """版本不兼容要说清是版本问题，而不是含糊的 400。"""
    import httpx2
    from mcp.shared.exceptions import MCPError as SdkMcpError

    async with _serve() as url:
        async with httpx2.AsyncClient() as raw:
            response = await raw.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"MCP-Protocol-Version": "1999-01-01"},
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == -32600

        # 同一份错误体交给客户端分类，应给出可执行提示（升级 SDK / 放宽服务端版本）
        classified = McpToolClient(url=url, token="good-token")._classify(  # noqa: SLF001
            SdkMcpError(code=body["error"]["code"], message=body["error"]["message"])
        )

    assert isinstance(classified, McpProtocolError)
    assert "协议版本" in str(classified)
