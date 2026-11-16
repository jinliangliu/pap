# tests/unit/test_mcp_inbound_switch.py
"""对外通道总开关的响应形态。

这里只测「关掉之后回什么」：开关是否被读到属于集成层（`build_asgi` 里的
那一行），而回什么直接决定客户端会不会去改配置。

**为什么是 503 而不是 404**：404 会被当成「端点写错了」，于是人去检查
nginx 和路径；503 才说得清「服务在、通道被人关了」。
"""

from __future__ import annotations

import json

import pytest

from app.mcp_server.server import _respond_disabled

pytestmark = pytest.mark.anyio


async def test_returns_503_with_reason():
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await _respond_disabled({"type": "http"}, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503

    payload = json.loads(sent[1]["body"])
    assert payload["error"] == "mcp_channel_disabled"
    # 文案要能直接给人看：只回一个错误码，运维还得去翻代码。
    assert "管理员" in payload["message"]


async def test_ignores_non_http_scope():
    """lifespan / websocket 不该被这条路径挡掉，否则挂载握手就失败了。"""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await _respond_disabled({"type": "lifespan"}, send)

    assert sent == []
