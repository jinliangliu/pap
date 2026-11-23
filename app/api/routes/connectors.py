# app/api/routes/connectors.py
"""外部连接器的管理接口。

## 两级权限，不是一个

- **定义**（建 / 改 / 删 / 发现 / 绑定工具 / 健康检查）：平台管理员。
  连接器表是平台级的，改一次影响所有租户——端点换了，别人的工具也跟着换。
- **凭据**：`connector:write` 或平台管理员，写在自己所属的租户下。
  外部账号天然属于某个组织，这一层不该由平台代持。

分成两级不是为了复杂，是因为这两件事的风险不同：改端点能让所有人的数据
流向另一个地址；配凭据只影响自己这个组织。

## 为什么读接口不藏数据

列表会告诉调用者「本租户配没配凭据、这个连接器下有几个工具」。
知道有哪些连接器存在，本身不构成越权——真正的门在凭据与调用时的权限判定。
反过来藏起来会让「我配了但它不显示」变成一个查不动的问题。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.connector import HEALTH_UNKNOWN, Connector, ConnectorAuthType
from app.models.mcp_registered import McpRegisteredTool
from app.schemas.connector import (
    ConnectorBindRequest,
    ConnectorBindResponse,
    ConnectorCreateRequest,
    ConnectorCredentialRequest,
    ConnectorCredentialResponse,
    ConnectorDiscoverResponse,
    ConnectorHealthResponse,
    ConnectorItem,
    ConnectorListResponse,
    ConnectorRemoteTool,
    ConnectorUpdateRequest,
)
from app.services import connector_service
from app.services.audit_service import write_audit_event
from app.services.connector_service import ConnectorConfigError
from app.tenancy.context import tenant_scope
from app.tools.dependencies import get_tool_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connectors", tags=["connectors"])

CREDENTIAL_PERMISSION = "connector:write"


def _is_dev() -> bool:
    """本地开发放行：dev 模式下没有平台管理员角色，否则连一个连接器都建不出来，
    而这层能力恰好最需要在本地验证。生产不走这个分支。"""
    return settings.app_env == "development" and settings.auth_mode == "dev"


def _require_definition_manage(identity: Identity) -> None:
    if _is_dev() or identity.is_platform_admin:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="需要平台管理员权限才能修改连接器定义。",
    )


def _require_credential_manage(identity: Identity) -> None:
    if _is_dev() or identity.is_platform_admin or CREDENTIAL_PERMISSION in identity.scopes:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"缺少 {CREDENTIAL_PERMISSION} 权限，无法配置连接器凭据。",
    )


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _get(session, ref: str) -> Connector:
    row = await connector_service.get_connector(session, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="连接器不存在。")
    return row


def _item(
    row: Connector,
    *,
    credential_set: bool,
    tool_count: int,
) -> ConnectorItem:
    return ConnectorItem(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description or "",
        kind=row.kind,
        base_url=row.base_url or "",
        auth_type=row.auth_type,
        auth_header_name=row.auth_header_name or "",
        timeout_seconds=int(row.timeout_seconds or 30),
        enabled=bool(row.enabled),
        health_status=row.health_status or "unknown",
        health_message=row.health_message or "",
        health_checked_at=row.health_checked_at,
        credential_set=credential_set,
        tool_count=tool_count,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------- 读 ----------------


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(
    identity: Identity = Depends(get_current_identity),
) -> ConnectorListResponse:
    """列出连接器，附本租户的凭据状态与工具数。

    `credential_set` 按**调用者所在租户**算。同一份列表，A 看到「已配置」、
    B 看到「未配置」，这是正确行为——凭据本来就各归各的。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows = await connector_service.list_connectors(session)
            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            counts = await connector_service.tool_counts(session)

            items = [
                _item(
                    row,
                    credential_set=row.id in credentials,
                    tool_count=counts.get(row.id, 0),
                )
                for row in rows
            ]

            return ConnectorListResponse(items=items)


@router.get("/{ref}", response_model=ConnectorItem)
async def get_connector(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorItem:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            counts = await connector_service.tool_counts(session, connector_ids=[row.id])

            return _item(
                row,
                credential_set=row.id in credentials,
                tool_count=counts.get(row.id, 0),
            )


# ---------------- 定义 ----------------


@router.post("", response_model=ConnectorItem, status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreateRequest,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorItem:
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            try:
                row = await connector_service.create_connector(
                    session,
                    name=payload.name,
                    display_name=payload.display_name,
                    description=payload.description,
                    kind=payload.kind,
                    base_url=payload.base_url,
                    auth_type=payload.auth_type,
                    auth_header_name=payload.auth_header_name,
                    timeout_seconds=payload.timeout_seconds,
                    actor_id=identity.user_id,
                )
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc

            await write_audit_event(
                session,
                event_type="connector_created",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="connector",
                resource_id=str(row.id),
                message=f"新建连接器 {row.name}",
                metadata={
                    "name": row.name,
                    "kind": row.kind.value,
                    "base_url": row.base_url,
                    "auth_type": row.auth_type.value,
                },
            )
            await session.refresh(row)
            await session.commit()

            return _item(row, credential_set=False, tool_count=0)


@router.patch("/{ref}", response_model=ConnectorItem)
async def update_connector(
    ref: str,
    payload: ConnectorUpdateRequest,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorItem:
    """改定义。改完立刻重注册下属工具——否则会出现「页面显示新地址、
    模型还在调旧地址」这种只有在调用失败时才会暴露的错。"""
    _require_definition_manage(identity)

    fields = {name: getattr(payload, name) for name in payload.model_fields_set}

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)

            try:
                row, before, after = await connector_service.update_connector(
                    session,
                    connector=row,
                    payload=fields,
                    actor_id=identity.user_id,
                )
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc

            if before:
                await write_audit_event(
                    session,
                    event_type="connector_updated",
                    tenant_id=identity.tenant_id,
                    actor_id=identity.user_id,
                    resource_type="connector",
                    resource_id=str(row.id),
                    message=f"修改连接器 {row.name}",
                    metadata={"name": row.name, "before": before, "after": after},
                )

            await session.commit()

            await connector_service.sync_connector_tools(
                session, get_tool_gateway(), connector=row
            )

            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            counts = await connector_service.tool_counts(session, connector_ids=[row.id])

            return _item(
                row,
                credential_set=row.id in credentials,
                tool_count=counts.get(row.id, 0),
            )


@router.delete("/{ref}")
async def delete_connector(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)

            try:
                await connector_service.delete_connector_guard(session, connector=row)
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc

            name = row.name
            connector_id = row.id

            # `delete_connector_guard` 已经保证下面没有工具挂着，所以这里
            # 不需要摘注册表。留着那个"顺手清理"反而危险：它会让人以为
            # 删除是安全的，而漏掉某个来源时没有任何提示。
            await session.delete(row)
            await write_audit_event(
                session,
                event_type="connector_deleted",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="connector",
                resource_id=str(connector_id),
                message=f"删除连接器 {name}",
                metadata={"name": name},
            )
            await session.commit()

            # 本租户的凭据当场清掉。别的租户的行跨不过 RLS 与 tenant_scope
            # 这两道墙，由维护任务逐租户扫（`purge_orphan_credentials`）；
            # 它们指向的连接器已经不存在，任何读取路径都拿不到，等一轮维护即可。
            await connector_service.purge_credentials(connector_id=connector_id)

            connector_service.invalidate_runtime(connector_id=connector_id)

    return {"message": f"连接器 {name} 已删除。"}


# ---------------- 凭据 ----------------

# 保存凭据后顺带复查的超时（秒）。
#
# 为什么要有上限：这一步最常见的场景恰恰是**端点配错了**，按连接器配置的
# 30 秒去等，用户会以为「保存」按钮卡住，然后去刷新页面——凭据其实早存进去了。
# 3 秒足够覆盖同机/同内网的正常往返，超时也只是把状态记成 error，不影响保存。
_CREDENTIAL_RECHECK_SECONDS = 3.0


@router.put("/{ref}/credential", response_model=ConnectorCredentialResponse)
async def set_credential(
    ref: str,
    payload: ConnectorCredentialRequest,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorCredentialResponse:
    """写入本租户的凭据。**接口永不回显凭据**，只回「已设置」。

    写完立刻复查一次连通性：健康状态是**上一次检查**的结论，凭据换了它就不成立了。
    不复查的表现是一个自相矛盾的卡片——「已配置凭据」配着「尚未配置凭据，无法校验」，
    而那句话是十几分钟前留下的。真去查日志的人才知道凭据已经存好了。
    """
    _require_credential_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)

            try:
                created = await connector_service.set_credential(
                    session,
                    connector=row,
                    tenant_id=identity.tenant_id,
                    secret=payload.secret,
                    username=payload.username,
                    actor_id=identity.user_id,
                )
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc

            await write_audit_event(
                session,
                event_type="connector_credential_set",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="connector",
                resource_id=str(row.id),
                message=f"{'设置' if created else '更新'}连接器 {row.name} 的凭据",
                # 只记动作，不记值。审计表长期保留，一个令牌写进去就等于永久泄露。
                metadata={"connector": row.name, "created": created},
            )

            # 复查失败不能连累保存：凭据已经在事务里，报「保存失败」会让人重填一遍。
            try:
                health_status, health_message = await connector_service.check_connector(
                    connector=row,
                    secret=payload.secret.strip(),
                    timeout_seconds=_CREDENTIAL_RECHECK_SECONDS,
                )
            except Exception:  # noqa: BLE001 - 复查是附加信息，不是保存的前置条件
                logger.exception(
                    "保存凭据后自动复查失败。",
                    extra={"event": "connector_credential_recheck_failed", "connector": row.name},
                )
                health_status = HEALTH_UNKNOWN
                health_message = "凭据已保存，但自动复查未完成，请手动点「检查」。"

            await connector_service.record_health(
                session, connector=row, status=health_status, message=health_message
            )
            await write_audit_event(
                session,
                event_type="connector_health_checked",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="connector",
                resource_id=str(row.id),
                message=f"检查连接器 {row.name}：{health_status}",
                metadata={"connector": row.name, "status": health_status, "auto": True},
                success=health_status != "error",
            )
            await session.commit()

            return ConnectorCredentialResponse(
                connector=row.name,
                credential_set=True,
                health_status=health_status,
                message=f"凭据已保存；{health_message}",
            )


@router.delete("/{ref}/credential", response_model=ConnectorCredentialResponse)
async def clear_credential(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorCredentialResponse:
    _require_credential_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            removed = await connector_service.clear_credential(
                session, connector=row, tenant_id=identity.tenant_id
            )

            if removed:
                await write_audit_event(
                    session,
                    event_type="connector_credential_cleared",
                    tenant_id=identity.tenant_id,
                    actor_id=identity.user_id,
                    resource_type="connector",
                    resource_id=str(row.id),
                    message=f"清除连接器 {row.name} 的凭据",
                    metadata={"connector": row.name},
                )
                # 上一次「连接正常」是用刚被清掉的那枚令牌测出来的，留着就是假信息。
                await connector_service.record_health(
                    session,
                    connector=row,
                    status=HEALTH_UNKNOWN,
                    message="凭据已清除，尚未复查。",
                )

            await session.commit()

            return ConnectorCredentialResponse(
                connector=row.name,
                credential_set=False,
                message="凭据已清除。" if removed else "本来就没有配置凭据。",
            )


# ---------------- 健康检查 ----------------


@router.post("/{ref}/check", response_model=ConnectorHealthResponse)
async def check_connector(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorHealthResponse:
    """主动检查一次连通性。

    用**调用者租户**的凭据去测：健康状态是给人看的「我这边能不能用」，
    拿别人的凭据测出「正常」，只会让人对着 401 反复怀疑自己。
    """
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            secret = credentials[row.id].secret if row.id in credentials else None

            health_status, message = await connector_service.check_connector(
                connector=row, secret=secret
            )
            await connector_service.record_health(
                session, connector=row, status=health_status, message=message
            )

            await write_audit_event(
                session,
                event_type="connector_health_checked",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="connector",
                resource_id=str(row.id),
                message=f"检查连接器 {row.name}：{health_status}",
                metadata={"connector": row.name, "status": health_status},
                success=health_status != "error",
            )
            await session.commit()

            return ConnectorHealthResponse(
                connector=row.name,
                status=health_status,
                message=message,
                checked_at=row.health_checked_at,
            )


# ---------------- 能力发现与绑定 ----------------


@router.post("/{ref}/discover", response_model=ConnectorDiscoverResponse)
async def discover_tools(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorDiscoverResponse:
    """列出远端提供哪些工具，并标出哪些已经接入。

    「发现」必须和「接入」用同一个端点、同一份凭据，否则报出来的错
    会指向另一个服务（`docs/MCP_INTEGRATION.md` §2.1 踩过）。
    """
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            secret = credentials[row.id].secret if row.id in credentials else None

            # 没有凭据就别发请求。发出去必然是 401，而 401 到这里已经变成
            # 502「网关或反向代理拦下了请求」——人照着这句话去查隧道，真实原因
            # 却是自己刚把凭据清空了。宁可在这里直说「先配凭据」。
            if row.auth_type != ConnectorAuthType.NONE and not secret:
                raise _bad_request(
                    connector_service.ConnectorConfigError(
                        "尚未配置凭据，无法发现工具。"
                        "请先在连接器上「配置凭据」，再点「发现工具」。"
                    )
                )

            try:
                tools = await connector_service.discover_remote(connector=row, secret=secret)
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc
            except Exception as exc:  # 远端错误原样转达，不包装成 500
                logger.warning(
                    "连接器能力发现失败。",
                    extra={"event": "connector_discover_failed", "connector": row.name},
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"无法从 {row.base_url} 获取工具清单：{exc}",
                ) from exc

            bound = {
                result.remote_name: result.local_name
                for result in (
                    await session.scalars(
                        select(McpRegisteredTool).where(
                            McpRegisteredTool.connector_id == row.id
                        )
                    )
                ).all()
            }

            return ConnectorDiscoverResponse(
                connector=row.name,
                endpoint=row.base_url,
                tools=[
                    ConnectorRemoteTool(
                        remote_name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.input_schema or {},
                        bound_local_name=bound.get(tool.name),
                    )
                    for tool in tools
                ],
            )


@router.post("/{ref}/tools", response_model=ConnectorBindResponse)
async def bind_tools(
    ref: str,
    payload: ConnectorBindRequest,
    identity: Identity = Depends(get_current_identity),
) -> ConnectorBindResponse:
    """批量接入远端工具。

    一次接一个连接器时要填二十次表单，没人会认真读第二十遍的风险等级。
    批量是**安全措施**，不是省事：它让「这个系统的接口都是只读的」
    只需要判断一次。
    """
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            credentials = await connector_service.credential_map(
                session, tenant_id=identity.tenant_id
            )
            secret = credentials[row.id].secret if row.id in credentials else None

            try:
                bound = await connector_service.bind_mcp_tools(
                    session,
                    get_tool_gateway(),
                    connector=row,
                    secret=secret,
                    items=[item.model_dump() for item in payload.items],
                    actor_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                )
            except ConnectorConfigError as exc:
                raise _bad_request(exc) from exc
            except Exception as exc:
                logger.warning(
                    "连接器工具接入失败。",
                    extra={"event": "connector_bind_failed", "connector": row.name},
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"接入失败：{exc}",
                ) from exc

            return ConnectorBindResponse(
                connector=row.name,
                bound=bound,
                message=f"已接入 {len(bound)} 个工具，立即生效。",
            )


@router.delete("/{ref}/tools/{local_name}")
async def unbind_tool(
    ref: str,
    local_name: str,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    _require_definition_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)
            target = await session.scalar(
                select(McpRegisteredTool).where(
                    McpRegisteredTool.local_name == local_name,
                    McpRegisteredTool.connector_id == row.id,
                )
            )

            if target is None:
                raise HTTPException(status_code=404, detail="这个连接器下没有该工具。")

            await connector_service.unbind_tool(
                session,
                get_tool_gateway(),
                row=target,
                connector=row,
                actor_id=identity.user_id,
                tenant_id=identity.tenant_id,
            )

            return {"message": f"已取消接入 {local_name}。"}


# ---------------- 供上层复用的查询 ----------------


@router.get("/{ref}/tools", response_model=list[dict])
async def list_bound_tools(
    ref: str,
    identity: Identity = Depends(get_current_identity),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    """连接器下已接入的工具。界面用它显示「这个连接器现在提供了什么」。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _get(session, ref)

            rows = (
                await session.scalars(
                    select(McpRegisteredTool)
                    .where(McpRegisteredTool.connector_id == row.id)
                    .order_by(McpRegisteredTool.local_name)
                    .limit(limit)
                )
            ).all()

            return [
                {
                    "local_name": item.local_name,
                    "remote_name": item.remote_name,
                    "display_name": item.display_name,
                    "risk_level": item.risk_level.value,
                    "enabled": bool(item.enabled),
                    "registered": get_tool_gateway().has_tool(item.local_name),
                }
                for item in rows
            ]


__all__ = ["router"]
