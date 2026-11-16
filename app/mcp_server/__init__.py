# app/mcp_server/__init__.py
"""入向 MCP 通道：PAP 作为服务端被外部宿主调用（docs/MCP_SERVER.md）。

方向不要搞错：`app/tools/mcp_tools.py` 是出向（PAP 当客户端去连别人），
这里是入向（WorkBuddy 连接器把事委派进来）。
"""

from app.mcp_server.server import (
    build_asgi,
    get_server,
    get_session_manager,
    reset_for_tests,
)

__all__ = [
    "build_asgi",
    "get_server",
    "get_session_manager",
    "reset_for_tests",
]
