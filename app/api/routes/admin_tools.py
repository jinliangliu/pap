# app/api/routes/admin_tools.py
"""自定义 HTTP Tool 的管理通道。

与 `/admin/mcp` 的只读探测不同，这里会真正落库并改变模型可见能力，
因此每个写操作都写 ADMIN_ACTION 审计，且只对 platform_admin 开放。

范围说明：
- 只允许新建 HTTP Tool。MCP Tool 的接入走 `/admin/mcp/tools`
  （远端必须真实存在，schema 取自远端），Python 工具是代码注册的，
  二者都不可能由一条 JSON 配置凭空产生；
- 导入导出只覆盖自定义工具，不包含内置与 MCP 工具——把内置实现导出成
  一份「看起来能再导入」的文件只会误导人。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_platform_admin
from app.api.routes.admin import _audit
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.custom_tool import CustomTool
from app.schemas.custom_tool import (
    CustomToolCreate,
    CustomToolExportResponse,
    CustomToolImportRequest,
    CustomToolImportResponse,
    CustomToolListResponse,
    CustomToolResponse,
    CustomToolUpdate,
    ParseCurlRequest,
    ParseCurlResponse,
    ParseUrlRequest,
    ParseUrlResponse,
    ToolParamIn,
)
from app.services.custom_tool_service import (
    ensure_name_available,
    export_tools,
    import_tools,
    parse_curl,
    parse_url_params,
    register_row,
    row_to_response,
    unregister_row,
    validate_payload,
)
from app.tenancy.context import platform_admin_tenant_scope
from app.tools.custom_http import CustomToolConfigError
from app.tools.dependencies import get_tool_gateway
from app.tools.gateway import ToolGatewayError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/tools", tags=["admin-tools"])

_SCOPE = "platform"


def _registered_total(gateway) -> int:
    return sum(1 for tool in gateway.list_tools(include_disabled=True) if tool.source == "custom")


async def _get_row(session: AsyncSession, tool_id: int) -> CustomTool:
    row = await session.get(CustomTool, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工具不存在。")
    return row


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=CustomToolListResponse)
async def list_custom_tools(
    identity: Identity = Depends(require_platform_admin),
) -> CustomToolListResponse:
    gateway = get_tool_gateway()

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            rows = (
                await session.scalars(select(CustomTool).order_by(CustomTool.id))
            ).all()

    return CustomToolListResponse(
        items=[row_to_response(row, gateway=gateway) for row in rows],
        total=len(rows),
        registered_total=_registered_total(gateway),
    )


@router.get("/export", response_model=CustomToolExportResponse)
async def export_custom_tools(
    identity: Identity = Depends(require_platform_admin),
) -> CustomToolExportResponse:
    """导出全部自定义工具定义（不含任何凭据）。"""
    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            rows = (
                await session.scalars(select(CustomTool).order_by(CustomTool.id))
            ).all()

    return CustomToolExportResponse(
        exported_at=datetime.now(UTC),
        tools=export_tools(list(rows)),
    )


@router.post("/parse-url", response_model=ParseUrlResponse)
async def parse_url(
    payload: ParseUrlRequest,
    identity: Identity = Depends(require_platform_admin),
) -> ParseUrlResponse:
    """从 URL 里的占位符反解 path 参数，只解析不保存。"""
    try:
        params, normalized = parse_url_params(payload.url)
    except CustomToolConfigError as exc:
        raise _bad_request(exc) from exc

    return ParseUrlResponse(url=normalized, params=params)


@router.post("/parse-curl", response_model=ParseCurlResponse)
async def parse_curl_command(
    payload: ParseCurlRequest,
    identity: Identity = Depends(require_platform_admin),
) -> ParseCurlResponse:
    """从 curl 命令提取方法、URL、请求头与 body 参数。

    不执行命令；含凭据参数（-u / --cookie）时直接拒绝，
    因为凭据不该出现在工具配置里。
    """
    try:
        parsed = parse_curl(payload.curl)
    except CustomToolConfigError as exc:
        raise _bad_request(exc) from exc

    return ParseCurlResponse(
        method=parsed["method"],
        url=parsed["url"],
        params=[ToolParamIn(**item) for item in parsed["params"]],
        headers=parsed["headers"],
        dropped_headers=parsed["dropped_headers"],
        has_body=parsed["has_body"],
    )


@router.post("", response_model=CustomToolResponse, status_code=201)
async def create_custom_tool(
    payload: CustomToolCreate,
    identity: Identity = Depends(require_platform_admin),
) -> CustomToolResponse:
    gateway = get_tool_gateway()

    try:
        validate_payload(payload)
        ensure_name_available(gateway=gateway, name=payload.name)
    except (CustomToolConfigError, ToolGatewayError) as exc:
        raise _bad_request(exc) from exc

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                exists = await session.scalar(
                    select(CustomTool).where(CustomTool.name == payload.name)
                )
                if exists is not None:
                    raise HTTPException(status_code=409, detail="同名工具已存在。")

                row = CustomTool(
                    name=payload.name,
                    display_name=payload.display_name,
                    description=payload.description,
                    method=payload.method,
                    url=payload.url,
                    risk_level=payload.risk_level,
                    timeout_seconds=payload.timeout_seconds,
                    params=[entry.model_dump() for entry in payload.params],
                    headers=payload.headers,
                    enabled=payload.enabled,
                    created_by=identity.subject,
                )
                session.add(row)
                await session.flush()

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_SCOPE,
                    operation="custom_tool_created",
                    resource_type="custom_tool",
                    resource_id=str(row.id),
                    metadata={
                        "name": row.name,
                        "method": row.method,
                        "url": row.url,
                        "risk_level": row.risk_level.value,
                    },
                )

    registered = register_row(gateway, row)
    if not registered:
        logger.error(
            "自定义工具已保存但注册失败。",
            extra={"event": "custom_tool_register_failed", "tool_name": row.name},
        )

    return row_to_response(row, gateway=gateway)


@router.put("/{tool_id}", response_model=CustomToolResponse)
async def update_custom_tool(
    tool_id: int,
    payload: CustomToolUpdate,
    identity: Identity = Depends(require_platform_admin),
) -> CustomToolResponse:
    """更新配置。name 不可改：它已经写进审计与审批记录。"""
    gateway = get_tool_gateway()
    data = payload.model_dump(exclude_unset=True)

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                row = await _get_row(session, tool_id)

                merged = CustomToolCreate(
                    name=row.name,
                    display_name=data.get("display_name", row.display_name),
                    description=data.get("description", row.description),
                    method=data.get("method", (row.method or "GET").upper()),
                    url=data.get("url", row.url),
                    risk_level=data.get("risk_level", row.risk_level),
                    timeout_seconds=data.get("timeout_seconds", row.timeout_seconds),
                    params=data.get(
                        "params",
                        [
                            ToolParamIn(**entry)
                            for entry in (row.params or [])
                            if isinstance(entry, dict)
                        ],
                    ),
                    headers=data.get("headers", row.headers or {}),
                    enabled=data.get("enabled", row.enabled),
                )

                try:
                    validate_payload(merged)
                except CustomToolConfigError as exc:
                    raise _bad_request(exc) from exc

                row.display_name = merged.display_name
                row.description = merged.description
                row.method = merged.method
                row.url = merged.url
                row.risk_level = merged.risk_level
                row.timeout_seconds = merged.timeout_seconds
                row.params = [entry.model_dump() for entry in merged.params]
                row.headers = merged.headers
                row.enabled = merged.enabled
                row.updated_at = datetime.now(UTC)

                await session.flush()

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_SCOPE,
                    operation="custom_tool_updated",
                    resource_type="custom_tool",
                    resource_id=str(row.id),
                    metadata={
                        "name": row.name,
                        "method": row.method,
                        "url": row.url,
                        "enabled": row.enabled,
                    },
                )

    if row.enabled:
        register_row(gateway, row)
    else:
        # 停用要真的从注册表摘掉，否则模型还能看到并调用它。
        unregister_row(gateway, row.name)

    return row_to_response(row, gateway=gateway)


@router.delete("/{tool_id}")
async def delete_custom_tool(
    tool_id: int,
    identity: Identity = Depends(require_platform_admin),
) -> dict:
    gateway = get_tool_gateway()

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                row = await _get_row(session, tool_id)
                name = row.name
                await session.delete(row)
                await session.flush()

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_SCOPE,
                    operation="custom_tool_deleted",
                    resource_type="custom_tool",
                    resource_id=str(tool_id),
                    metadata={"name": name},
                )

    unregister_row(gateway, name)

    return {"deleted": True, "name": name, "message": f"已删除工具 {name}。"}


@router.post("/import", response_model=CustomToolImportResponse)
async def import_custom_tools(
    payload: CustomToolImportRequest,
    identity: Identity = Depends(require_platform_admin),
) -> CustomToolImportResponse:
    """导入工具定义。逐条处理，单条失败不影响其余条目。"""
    gateway = get_tool_gateway()

    with platform_admin_tenant_scope(_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                result = await import_tools(
                    session,
                    gateway,
                    payload=payload,
                    actor=identity.subject,
                )

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_SCOPE,
                    operation="custom_tool_imported",
                    resource_type="custom_tool",
                    resource_id="batch",
                    metadata={
                        "mode": payload.mode,
                        "created": result.created,
                        "updated": result.updated,
                        "skipped": result.skipped,
                    },
                )

    return result
