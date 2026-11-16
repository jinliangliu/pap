# app/mcp_server/server.py
"""对外 MCP 服务端的装配。

**这个模块和 `app/tools/mcp_tools.py` 方向相反。** 那边 PAP 是客户端，
去连别人的 MCP 服务；这边 PAP 是服务端，被别的宿主（WorkBuddy 连接器）
调用。两边都叫 MCP，别混。

三条实现约束：

1. **stateless**。每个请求自带凭据、不建会话状态。有状态会话在
   `systemctl restart pap-api` 之后全废，而连接器不知道要重新 `initialize`，
   表现是「连接器显示已连接，但每次调用都失败」——平台已经踩过一次
   「界面正常、后端没数据」，不要再来一次。
2. **json 响应而不是 SSE**。现阶段工具调用没有进度事件，SSE 只多出
   「反代把它缓冲掉」的风险。要推进度事件时关掉 `mcp_inbound_json_response`，
   同时必须在反代上 `proxy_buffering off`。
3. **关掉 DNS rebinding 保护**。SDK 默认只放行 127.0.0.1/localhost 的 Host，
   挂在真实域名后面会直接 421。那道保护防的是「浏览器里的恶意页面拿用户
   cookie 打本地端口」——我们不用 cookie，凭据是显式 Bearer 头，
   浏览器不会自动携带。真正挡住匿名访问的是 `BearerGate`。

**会话管理器必须由我们自己 run。** `streamable_http_app()` 返回的 Starlette
应用自带 lifespan，但被 FastAPI `mount` 之后它的 lifespan 不会执行
（Starlette 不往子应用传 lifespan）。所以这里只取出它内部的
`session_manager`，在 `app/main.py` 的 lifespan 里 `async with run()`，
再把 `handle_request` 挂到路由上。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.core.config import settings
from app.mcp_server.auth import BearerGate
from app.mcp_server.tools import register_tools
from app.mcp_server.upload import handle_upload, is_upload_path
from app.services import runtime_settings

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "PAP 是多智能体平台：把一整件事交给它，由它自己拆解、选智能体、调数据源。"
    "一次只交一件事，不要预先拆步骤，也不要替它指定工具。"
    "返回 status 不是 completed 时按 hint 处理：needs_input 要把问题带回给用户，"
    "awaiting_approval 要请用户去 PAP 控制台确认，running 要用 pap_task_status 取回。"
    # 这一段是给「交文件」用的。写在这里而不是只写在文档里，是因为模型看不到
    # 仓库文档——它只看得到工具表与本段说明。没有这段，模型遇到「把这个文件交给
    # PAP」会先去读文件、再把正文塞进 task：既烧上下文，又等于让模型复述一遍原文。
    "有本地文件要交给 PAP（入库、存档、按内容问答）时，不要读取文件内容再塞进 task，"
    "改用 multipart 提交到本端点下的 /upload（文件字段 file，可选 title 与 "
    "access_scope），令牌与调用本服务相同；一次可传多个文件，各自提交。"
    "返回 action 为 created / updated / unchanged，unchanged 表示同一份内容已在库里、"
    "没有重复入库。"
)

_server: MCPServer | None = None
_session_manager: Any = None


def get_server() -> MCPServer:
    """进程内唯一的 MCP 服务端实例。

    只构造一次的理由不是省内存，而是工具表必须唯一：重复注册会出现
    「同一个名字两个实现」，而调用方看到的只有先注册的那个。
    """
    global _server

    if _server is None:
        _server = MCPServer(
            name="pap",
            title="PAP 智能体平台",
            description="多智能体平台：一次委派拿到结论、引用与执行轨迹。",
            instructions=INSTRUCTIONS,
            version=settings.app_version,
        )
        register_tools(_server)

    return _server


def build_asgi() -> Any:
    """构造挂在 `/mcp` 上的 ASGI 应用，并取出需要自己驱动的会话管理器。

    这个函数必须在 `create_app()` 里调用一次（挂载需要它），
    但 `run()` 只能在 lifespan 里调一次，所以两者拆开。
    """
    global _session_manager

    server = get_server()

    # 返回的 Starlette 应用本身不用，要的是它初始化出来的 session_manager。
    server.streamable_http_app(
        streamable_http_path="/",
        json_response=settings.mcp_inbound_json_response,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    _session_manager = server.session_manager

    protocol = _session_manager.handle_request

    async def router(scope, receive, send) -> None:
        # 通道总开关在分流**之前**：管理员关掉「外部通道接入」时，
        # 上传入口也要一起关。只挡 JSON-RPC 会把上传留成一条能写库的缝。
        #
        # 返回 503 而不是 404：404 会被客户端当成「端点写错了」，
        # 于是人去改配置；503 才说得清「服务在、通道被人关了」。
        if not runtime_settings.mcp_inbound_enabled():
            await _respond_disabled(scope, send)
            return

        # `/mcp/upload` 不是 JSON-RPC，交给上传处理器。分流放在门槛**内侧**：
        # 上传同样要过 BearerGate，否则它会变成一条无鉴权的写入口。
        if scope.get("type") == "http" and is_upload_path(scope.get("path") or ""):
            await handle_upload(scope, receive, send)
            return

        await protocol(scope, receive, send)

    return BearerGate(router)


async def _respond_disabled(scope: dict, send: Any) -> None:
    """通道关闭时的最小 HTTP 响应。

    lifespan 之类的非 http scope 不处理：它们不是「外部系统在调我」，
    挡掉只会让挂载时的握手失败。
    """
    if scope.get("type") != "http":
        return

    body = json.dumps(
        {
            "error": "mcp_channel_disabled",
            "message": "外部通道已由管理员关闭。",
        },
        ensure_ascii=False,
    ).encode()

    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def get_session_manager() -> Any:
    return _session_manager


def reset_for_tests() -> None:
    """测试用：清掉进程内单例。

    `StreamableHTTPSessionManager.run()` 每个实例只能调一次，
    测试里重建应用必须换一个新实例。
    """
    global _server, _session_manager

    _server = None
    _session_manager = None
