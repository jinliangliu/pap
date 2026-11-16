# app/services/mcp_tool_service.py
"""管理员在控制台接入的远端 MCP Tool。

职责边界（与 `app/tools/mcp_tools.py` 的分工）：
- 本模块只管「行 → spec → 注册表」这一段，以及名字冲突的判定；
- 远端 schema、调用、结果截断仍由 mcp_tools 负责，这里不重复实现。

三条约束：
1. 代码白名单优先。同名时忽略配置而不是覆盖——代码里的定义带审批语义；
2. 落库后必须立刻进注册表。列表显示「已接入」而模型调不到，是最难查的错；
3. 取消接入必须真的从注册表摘掉，否则模型仍能调用一个库里已没有的工具。

地址与令牌跟着接入项走（`server_url` / `token`）：MCP 不再假设只有一条通道，
但「发现」与「接入」必须对同一个端点，否则报出来的错会指向错误的服务。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.approval import RiskLevel
from app.models.connector import Connector
from app.models.mcp_registered import McpRegisteredTool
from app.schemas.admin import McpRegisteredToolResponse
from app.tools.gateway import ToolGateway, ToolGatewayError
from app.tools.mcp_tools import (
    McpToolSpec,
    build_dynamic_definition,
    code_local_names,
    endpoint_key,
)

logger = logging.getLogger(__name__)

_WRITE_RISKS = frozenset(
    {
        RiskLevel.WRITE,
        RiskLevel.EXECUTE,
        RiskLevel.DESTRUCTIVE,
        RiskLevel.EXTERNAL,
    }
)

_SOURCE_LABEL = {"builtin": "内置", "custom": "自定义", "mcp": "MCP"}


class McpToolNameError(ValueError):
    """工具名不可用（与代码白名单或其它工具冲突）。"""


def requires_approval(risk_level: RiskLevel) -> bool:
    """与 ToolGateway 同一套判定，前端展示用。"""
    return risk_level in _WRITE_RISKS


def row_to_spec(
    row: McpRegisteredTool,
    *,
    connector: Connector | None = None,
) -> McpToolSpec:
    """行 → spec。带连接器时端点与令牌的语义完全由连接器接管。

    `server_url` 填连接器的地址**只用于注册时拉 schema**（`fetch_remote_tools`
    需要知道去哪个端点问）。真正的业务调用走 `context.connector`，
    令牌取调用方租户自己的那一份。
    """
    base = McpToolSpec(
        local_name=row.local_name,
        remote_name=row.remote_name,
        display_name=row.display_name,
        description=row.description or "",
        risk_level=row.risk_level,
        # 审批由风险等级单点推导，表里不存这个字段——两处都存早晚会不一致。
        requires_approval=requires_approval(row.risk_level),
        timeout_seconds=int(row.timeout_seconds or 60),
        # 端点与令牌跟着接入项走：同一条通道的假设在接入第二个服务时不成立。
        server_url=row.server_url or None,
        token=row.token or None,
    )

    if connector is None:
        return base

    return replace(
        base,
        # 连接器的地址与行上的地址同时存在时以连接器为准（见模型注释）。
        server_url=connector.base_url or None,
        # 令牌不在这里：它是租户级的，spec 是进程级的，装进来就会串租户。
        token=None,
        connector_id=connector.id,
        connector_name=connector.name,
        connector_ready=bool(connector.enabled),
    )


def row_to_response(
    row: McpRegisteredTool,
    *,
    gateway: ToolGateway | None = None,
) -> McpRegisteredToolResponse:
    registered = False
    if gateway is not None and gateway.has_tool(row.local_name):
        # 同名被代码实现占住时不算「已注册成功」：模型调到的是另一份定义。
        registered = gateway.get_tool(row.local_name).source == "mcp"

    return McpRegisteredToolResponse(
        id=row.id,
        local_name=row.local_name,
        remote_name=row.remote_name,
        display_name=row.display_name,
        description=row.description or "",
        risk_level=row.risk_level,
        requires_approval=requires_approval(row.risk_level),
        timeout_seconds=int(row.timeout_seconds or 60),
        enabled=bool(row.enabled),
        registered=registered,
        # 地址可以给：它出现在 .env 里，不是秘密。令牌只回「有没有」。
        server_url=row.server_url or None,
        token_set=bool(row.token),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def ensure_name_allowed(local_name: str, *, gateway: ToolGateway) -> None:
    """名字必须先过这一关，再谈落库。

    代码登记的工具不允许被配置覆盖：那是审批路径上的实现，一条 JSON
    配置就能替换掉它，等于把审批绕开的开关交给了配置面。
    """
    if local_name in code_local_names():
        raise McpToolNameError(
            f"{local_name} 已在代码白名单里，无需接入，也不允许用配置覆盖。"
        )

    if not gateway.has_tool(local_name):
        return

    existing = gateway.get_tool(local_name)
    if existing.source != "mcp":
        raise McpToolNameError(
            f"工具名 {local_name} 已被{_SOURCE_LABEL.get(existing.source, '其它')}工具占用。"
        )


async def load_registered_specs(session: AsyncSession) -> tuple[McpToolSpec, ...]:
    """启动时读出启用中的接入项。坏一条不影响其它条。

    **只返回没有连接器的行**：带连接器的 MCP 工具由
    `connector_service.load_connector_tools` 注册，它需要按连接器取一份
    租户凭据才能拉到远端 schema。放在这里注册的话，会因为拿不到令牌
    而在日志里刷一串"远端不可达"——工具本身是好的，只是走错了入口。
    """
    rows = (
        await session.scalars(
            select(McpRegisteredTool)
            .where(
                McpRegisteredTool.enabled.is_(True),
                McpRegisteredTool.connector_id.is_(None),
            )
            .order_by(McpRegisteredTool.id)
        )
    ).all()
    return tuple(row_to_spec(row) for row in rows)


async def admin_registered_map(
    session: AsyncSession,
    *,
    server_url: str | None = None,
) -> dict[str, str]:
    """远端名 → 本地名（含停用项）。

    「发现」结果要靠这份映射标出哪些是管理员接入的（可以取消），
    代码白名单不在里面——那些只能改代码下线。

    给了 `server_url` 就只算这个端点上的接入项：同一个远端名可能出现在
    两个服务里（都叫 `ocr_file`），不按端点过滤会把 A 的接入状态标到 B 上。
    为空表示默认端点（含 v1 那种没记地址的存量行）。
    """
    rows = await session.scalars(
        select(McpRegisteredTool).order_by(McpRegisteredTool.id)
    )
    default_key = endpoint_key(settings.mcp_server_url)
    target_key = endpoint_key(server_url) if server_url else default_key

    mapping: dict[str, str] = {}
    for row in rows:
        if endpoint_key(row.server_url or settings.mcp_server_url) != target_key:
            continue
        mapping[row.remote_name] = row.local_name
    return mapping


def register_row(
    gateway: ToolGateway,
    row: McpRegisteredTool,
    descriptor,
    *,
    connector: Connector | None = None,
) -> bool:
    """注册（或覆盖）单条接入项。失败返回 False，不抛。

    `connector` 必须由调用方给：这行的 `connector_id` 只是个整数，
    查连接器要一次额外查询，而这个函数在启动与同步路径上被批量调用，
    让它自己查会变成 N+1。忘了传的后果是工具按"独立工具"注册——
    端点与凭据都错，所以调用方一律显式传。
    """
    try:
        gateway.register_tool(
            build_dynamic_definition(row_to_spec(row, connector=connector), descriptor=descriptor),
            replace=True,
        )
        return True
    except (ToolGatewayError, ValueError):
        logger.exception(
            "注册 MCP 工具失败，该项本次不可用。",
            extra={"event": "mcp_registered_tool_failed", "tool_name": row.local_name},
        )
        return False


def unregister_row(gateway: ToolGateway, local_name: str) -> None:
    try:
        gateway.unregister_mcp_tool(local_name)
    except ToolGatewayError:
        logger.warning(
            "取消接入时发现该工具不是 MCP 来源，已跳过。",
            extra={"event": "mcp_unregister_skipped", "tool_name": local_name},
        )
