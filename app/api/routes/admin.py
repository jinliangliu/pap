# app/api/routes/admin.py
"""平台管理通道（跨租户）。

默认禁止任何跨租户查询；只有同时满足下面三点才能操作：

1. 身份是平台租户里的 `platform_admin`；
2. 进入 `platform_admin_tenant_scope(target)` —— 代码里必须显式写出目标租户；
3. 每个写操作都写 `ADMIN_ACTION` 审计（谁、对哪个租户、做了什么）。

管理通道只管理「租户、成员、配额、工具策略、审计」，
不提供读取租户会话内容或知识库原文的接口：运维便利不能变成数据后门。
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_platform_admin
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.integrations.mcp_client import McpError
from app.models.audit import AuditLog
from app.models.connector import Connector, ConnectorCredential
from app.models.mcp_registered import McpRegisteredTool
from app.models.tenant import Tenant
from app.observability.writer import get_writer as get_obs_writer
from app.repositories.model_provider import ModelProviderRepository
from app.schemas.admin import (
    AuditItem,
    McpDiscoverRequest,
    McpDiscoverResponse,
    McpRegisteredToolListResponse,
    McpRegisteredToolResponse,
    McpRemoteTool,
    McpRuntimeResponse,
    McpToolRegisterRequest,
    McpToolUpdateRequest,
    MemberResponse,
    MemberUpsertRequest,
    PolicySnapshotResponse,
    QuotaRequest,
    QuotaResponse,
    RuntimeSettingsResponse,
    RuntimeSettingsUpdate,
    TenantCreateRequest,
    TenantResponse,
    TenantStatusRequest,
    ToolPolicyRequest,
    ToolPolicyResponse,
)
from app.schemas.model_provider import (
    DiscoverModelsRequest,
    DiscoverModelsResponse,
    EffectiveModelResponse,
    ProviderCreate,
    ProviderListResponse,
    ProviderResponse,
    ProviderUpdate,
)
from app.services import connector_service, runtime_settings
from app.services.audit_service import write_admin_action
from app.services.mcp_tool_service import (
    McpToolNameError,
    admin_registered_map,
    ensure_name_allowed,
    register_row,
    row_to_response,
    unregister_row,
)
from app.services.model_service import invalidate_model_cache, resolve_text_model
from app.services.tenant_service import get_tenant_service
from app.tenancy.context import platform_admin_tenant_scope
from app.tools.dependencies import get_tool_gateway
from app.tools.gateway import is_write_risk, risk_override_error
from app.tools.mcp_tools import (
    McpConfigError,
    code_local_names,
    endpoint_key,
    fetch_remote_tools,
    remote_to_local_map,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _tenant_response(tenant: Tenant) -> TenantResponse:
    return TenantResponse(
        tenant_id=tenant.id,
        name=tenant.name,
        status=tenant.status,
        created_at=tenant.created_at,
    )


async def _audit(
    session: AsyncSession,
    *,
    identity: Identity,
    target_tenant: str,
    operation: str,
    resource_type: str,
    resource_id: str,
    metadata: dict | None = None,
) -> None:
    await write_admin_action(
        session,
        actor_id=identity.user_id,
        actor_subject=identity.subject,
        actor_tenant=identity.tenant_id,
        target_tenant=target_tenant,
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
    )


@router.get("/runtime-settings", response_model=RuntimeSettingsResponse)
async def get_runtime_settings(
    identity: Identity = Depends(require_platform_admin),
) -> RuntimeSettingsResponse:
    """当前运行配置。只读，任何人都能看（平台管理员之外看不到这个路由）。

    返回值里带 `source`：`runtime` 表示管理员设过、库里查得到；
    `env` 表示还没有记录，用的是 `.env` 的默认值。页面据此提示。
    """
    return RuntimeSettingsResponse(**runtime_settings.snapshot())


@router.put("/runtime-settings", response_model=RuntimeSettingsResponse)
async def update_runtime_settings(
    payload: RuntimeSettingsUpdate,
    identity: Identity = Depends(require_platform_admin),
) -> RuntimeSettingsResponse:
    """改运行配置，**立刻生效**，不重启进程。

    观测开关要动两处：先写配置（库 + 进程内存），再让写入器起停后台任务。
    顺序不能反——`start()` 会再读一次开关，先起任务再改配置会被判成「关」。

    两个字段都是「不传 = 不动」：只想改容量时不必把观测开关的当前值抄一遍。
    """
    toggled = payload.observability_enabled is not None
    previous = runtime_settings.obs_enabled() if toggled else None

    if toggled:
        await runtime_settings.set_observability(
            payload.observability_enabled, actor=identity.subject
        )
        await get_obs_writer().set_enabled(payload.observability_enabled)

    # 切片上限只写配置，没有第二个落点。
    chunk_limit_changed = False
    if payload.knowledge_max_chunks is not None:
        chunk_limit_changed = await runtime_settings.set_knowledge_max_chunks(
            payload.knowledge_max_chunks, actor=identity.subject
        )

    # 备份三项：各自独立落审计。塞进同一条会让「谁把阈值调小了」查不出来。
    backup_changes: list[tuple[str, Any]] = []
    if payload.tool_backup_enabled is not None:
        if await runtime_settings.set_backup_enabled(
            payload.tool_backup_enabled, actor=identity.subject
        ):
            backup_changes.append(
                (runtime_settings.TOOL_BACKUP_ENABLED, payload.tool_backup_enabled)
            )
    if payload.tool_backup_threshold_mb is not None:
        if await runtime_settings.set_backup_threshold_mb(
            payload.tool_backup_threshold_mb, actor=identity.subject
        ):
            backup_changes.append(
                (runtime_settings.TOOL_BACKUP_THRESHOLD_MB, payload.tool_backup_threshold_mb)
            )
    if payload.tool_backup_max_size_mb is not None:
        if await runtime_settings.set_backup_max_size_mb(
            payload.tool_backup_max_size_mb, actor=identity.subject
        ):
            backup_changes.append(
                (runtime_settings.TOOL_BACKUP_MAX_SIZE_MB, payload.tool_backup_max_size_mb)
            )

    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            async with session.begin():
                if toggled:
                    await _audit(
                        session,
                        identity=identity,
                        target_tenant="platform",
                        operation="runtime_settings_updated",
                        resource_type="runtime_settings",
                        resource_id=runtime_settings.OBSERVABILITY_ENABLED,
                        metadata={
                            "observability_enabled": payload.observability_enabled,
                            "previous": previous,
                        },
                    )
                if chunk_limit_changed:
                    # 单独一条审计：它和观测开关是两件事，塞进上一条的
                    # metadata 里会让「谁在什么时候放宽了容量」查不出来。
                    await _audit(
                        session,
                        identity=identity,
                        target_tenant="platform",
                        operation="runtime_settings_updated",
                        resource_type="runtime_settings",
                        resource_id=runtime_settings.KNOWLEDGE_MAX_CHUNKS,
                        metadata={
                            "knowledge_max_chunks": payload.knowledge_max_chunks,
                        },
                    )
                for key, value in backup_changes:
                    await _audit(
                        session,
                        identity=identity,
                        target_tenant="platform",
                        operation="runtime_settings_updated",
                        resource_type="runtime_settings",
                        resource_id=key,
                        metadata={key: value},
                    )

    # 返回的是**改完之后**的快照：页面拿它直接回填，且能看见新的 revision。
    # 不传 revision 让人自己去查，会让「刚才那次保存到底生效了没有」变成
    # 一次额外请求。
    return RuntimeSettingsResponse(**runtime_settings.snapshot())


@router.get(
    "/runtime-settings/snapshots/{revision}",
    response_model=PolicySnapshotResponse,
)
async def get_policy_snapshot(
    revision: int,
    identity: Identity = Depends(require_platform_admin),
) -> PolicySnapshotResponse:
    """按号取回**当时**的完整生效配置。

    存在的意义是「事后能回答这一版是什么」：工具审计里记了 `policy_revision`，
    人拿着那个号就能在这里取回同一份值，而不是凭现在的配置去猜当时。

    沙箱侧**不需要**回头调这里：每次执行时那一份完整策略是随请求推过去的
    （见 `app/integrations/sandbox_client.py` 的模块文档），执行中途不再二次
    读取——否则两次读取之间管理员按一下开关，执行就跨越了两个策略。

    revision=0 永远有内容——它表示「从未改过配置，全按 .env 默认」，
    由代码合成而非落库。查不到时返回 `found=false` 而不是 404：
    「这一版不存在」与「这个接口不存在」对调用方是两个完全不同的处置。
    """
    snapshot = await runtime_settings.snapshot_at(revision)

    if snapshot is None:
        return PolicySnapshotResponse(revision=revision, settings={}, found=False)

    return PolicySnapshotResponse(revision=revision, settings=snapshot, found=True)


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    limit: int = Query(default=100, ge=1, le=500),
    identity: Identity = Depends(require_platform_admin),
) -> list[TenantResponse]:
    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(Tenant).order_by(Tenant.created_at.desc()).limit(limit)
            )
        ).all()

    return [_tenant_response(row) for row in rows]


@router.post("/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(
    payload: TenantCreateRequest,
    identity: Identity = Depends(require_platform_admin),
) -> TenantResponse:
    target = payload.tenant_id

    with platform_admin_tenant_scope(target):
        async with async_session_factory() as session:
            async with session.begin():
                try:
                    tenant = await get_tenant_service().create_tenant(
                        session,
                        tenant_id=target,
                        name=payload.name,
                        daily_chat_requests=payload.daily_chat_requests,
                        tool_calls_per_minute=payload.tool_calls_per_minute,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=target,
                    operation="tenant_created",
                    resource_type="tenant",
                    resource_id=target,
                    metadata={"name": payload.name},
                )

    return _tenant_response(tenant)


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse)
async def update_tenant_status(
    tenant_id: str,
    payload: TenantStatusRequest,
    identity: Identity = Depends(require_platform_admin),
) -> TenantResponse:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                try:
                    tenant = await get_tenant_service().set_status(
                        session,
                        tenant_id=tenant_id,
                        status=payload.status,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=tenant_id,
                    operation="tenant_status_changed",
                    resource_type="tenant",
                    resource_id=tenant_id,
                    metadata={"status": payload.status.value},
                )

    return _tenant_response(tenant)


@router.get("/tenants/{tenant_id}/members", response_model=list[MemberResponse])
async def list_members(
    tenant_id: str,
    identity: Identity = Depends(require_platform_admin),
) -> list[MemberResponse]:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            rows = await get_tenant_service().list_members(session, tenant_id=tenant_id)

    return [MemberResponse(**row) for row in rows]


@router.put("/tenants/{tenant_id}/members", response_model=MemberResponse)
async def upsert_member(
    tenant_id: str,
    payload: MemberUpsertRequest,
    identity: Identity = Depends(require_platform_admin),
) -> MemberResponse:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                try:
                    user, role = await get_tenant_service().add_member(
                        session,
                        tenant_id=tenant_id,
                        subject=payload.subject,
                        role_name=payload.role,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=tenant_id,
                    operation="member_upserted",
                    resource_type="user",
                    resource_id=str(user.id),
                    metadata={"subject": payload.subject, "role": role.name},
                )

    return MemberResponse(
        subject=user.external_subject,
        display_name=user.display_name,
        role=role.name,
        enabled=bool(user.enabled),
    )


@router.delete("/tenants/{tenant_id}/members/{subject}", status_code=204)
async def remove_member(
    tenant_id: str,
    subject: str,
    identity: Identity = Depends(require_platform_admin),
) -> None:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                removed = await get_tenant_service().remove_member(
                    session,
                    tenant_id=tenant_id,
                    subject=subject,
                )

                if not removed:
                    raise HTTPException(status_code=404, detail="成员不存在。")

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=tenant_id,
                    operation="member_removed",
                    resource_type="user",
                    resource_id=subject,
                )


@router.put("/tenants/{tenant_id}/quota", response_model=QuotaResponse)
async def set_quota(
    tenant_id: str,
    payload: QuotaRequest,
    identity: Identity = Depends(require_platform_admin),
) -> QuotaResponse:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                quota = await get_tenant_service().set_quota(
                    session,
                    tenant_id=tenant_id,
                    daily_chat_requests=payload.daily_chat_requests,
                    tool_calls_per_minute=payload.tool_calls_per_minute,
                    knowledge_max_documents=payload.knowledge_max_documents,
                )

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=tenant_id,
                    operation="quota_updated",
                    resource_type="tenant_quota",
                    resource_id=tenant_id,
                    metadata={
                        "daily_chat_requests": payload.daily_chat_requests,
                        "tool_calls_per_minute": payload.tool_calls_per_minute,
                    },
                )

    return QuotaResponse(
        tenant_id=quota.tenant_id,
        daily_chat_requests=quota.daily_chat_requests,
        tool_calls_per_minute=quota.tool_calls_per_minute,
        knowledge_max_documents=quota.knowledge_max_documents,
    )


@router.put("/tenants/{tenant_id}/tools/{tool_name}", response_model=ToolPolicyResponse)
async def set_tool_policy(
    tenant_id: str,
    tool_name: str,
    payload: ToolPolicyRequest,
    identity: Identity = Depends(require_platform_admin),
) -> ToolPolicyResponse:
    try:
        tool = get_tool_gateway().get_tool(tool_name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="工具未注册。") from exc

    # 兼容映射：老调用方只传 enabled。传了 action 就以 action 为准，
    # 因为三态里的「需确认」压不进一个布尔值。
    action = payload.action if payload.action is not None else (
        "allow" if payload.enabled else "deny"
    )

    reason = risk_override_error(tool, payload.risk_override)
    if reason:
        raise HTTPException(status_code=400, detail=reason)

    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                try:
                    policy = await get_tenant_service().set_tool_policy(
                        session,
                        tenant_id=tenant_id,
                        tool_name=tool_name,
                        action=action,
                        allowed_roles=payload.allowed_roles,
                        credential_ref=payload.credential_ref,
                        risk_override=payload.risk_override,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=tenant_id,
                    operation="tool_policy_updated",
                    resource_type="tenant_tool_policy",
                    resource_id=tool_name,
                    metadata={
                        "action": action,
                        "risk_override": payload.risk_override,
                        "allowed_roles": payload.allowed_roles,
                        "credential_ref": payload.credential_ref,
                    },
                )

    # action 非空时 set_tool_policy 一定返回行（只有「全空」才删行）。
    assert policy is not None
    return ToolPolicyResponse(
        tenant_id=policy.tenant_id,
        tool_name=policy.tool_name,
        enabled=policy.action != "deny",
        action=policy.action,
        risk_override=policy.risk_override,
        allowed_roles=list(policy.allowed_roles or []),
        credential_ref=policy.credential_ref,
    )


@router.get("/tenants/{tenant_id}/audit", response_model=list[AuditItem])
async def list_audit(
    tenant_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    identity: Identity = Depends(require_platform_admin),
) -> list[AuditItem]:
    with platform_admin_tenant_scope(tenant_id):
        async with async_session_factory() as session:
            rows = (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == tenant_id)
                    .order_by(AuditLog.created_at.desc())
                    .limit(limit)
                )
            ).all()

    return [
        AuditItem(
            id=str(row.id),
            event_type=row.event_type.value,
            actor_id=row.actor_id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            success=bool(row.success),
            message=row.message,
            metadata=row.metadata_json or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    identity: Identity = Depends(require_platform_admin),
) -> TenantResponse:
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)

    if tenant is None:
        raise HTTPException(status_code=404, detail="租户不存在。")

    return _tenant_response(tenant)


# ---------------- 模型供应商（文字模型服务） ----------------


def _effective_response(config) -> EffectiveModelResponse:
    return EffectiveModelResponse(
        source=config.source,
        provider_name=config.provider_name,
        protocol=config.protocol,
        model=config.model,
        base_url=config.base_url,
        temperature=config.temperature,
        reasoning_mode=config.reasoning_mode,
        api_key_set=bool(config.api_key),
    )


@router.get("/model-providers", response_model=ProviderListResponse)
async def list_model_providers(
    identity: Identity = Depends(require_platform_admin),
) -> ProviderListResponse:
    """供应商列表 + 当前实际生效的模型。

    effective 字段存在的意义：页面不必自己推断「到底用的是库里的还是 .env 的」。
    """
    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            rows = await ModelProviderRepository(session).list_all()

    config = await resolve_text_model()

    return ProviderListResponse(
        items=[ProviderResponse.from_row(row) for row in rows],
        effective=_effective_response(config),
    )


@router.post("/model-providers", response_model=ProviderResponse, status_code=201)
async def create_model_provider(
    payload: ProviderCreate,
    identity: Identity = Depends(require_platform_admin),
) -> ProviderResponse:
    data = payload.model_dump()
    data["models"] = [entry.model_dump() for entry in payload.models]
    audit_meta = {k: v for k, v in data.items() if k != "api_key"}

    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            async with session.begin():
                repo = ModelProviderRepository(session)
                if await repo.get_by_name(payload.name) is not None:
                    raise HTTPException(status_code=409, detail="同名供应商已存在。")

                row = await repo.create(data)
                await _audit(
                    session,
                    identity=identity,
                    target_tenant="platform",
                    operation="model_provider_created",
                    resource_type="model_provider",
                    resource_id=str(row.id),
                    metadata=audit_meta,
                )

    invalidate_model_cache()
    return ProviderResponse.from_row(row)


@router.put("/model-providers/{provider_id}", response_model=ProviderResponse)
async def update_model_provider(
    provider_id: int,
    payload: ProviderUpdate,
    identity: Identity = Depends(require_platform_admin),
) -> ProviderResponse:
    data = payload.model_dump(exclude_unset=True)
    if "models" in data and data["models"] is not None:
        data["models"] = [
            entry.model_dump() if hasattr(entry, "model_dump") else entry
            for entry in payload.models or []
        ]

    # 空字符串表示「清空密钥」，与「不修改」必须区分开：
    # 前端留空是「沿用」，显式清空是另一件事。
    clear_key = data.get("api_key") == ""
    if clear_key:
        data.pop("api_key")
    audit_meta = {k: v for k, v in data.items() if k != "api_key"}

    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            async with session.begin():
                repo = ModelProviderRepository(session)

                if payload.name is not None:
                    clash = await repo.get_by_name(payload.name)
                    if clash is not None and clash.id != provider_id:
                        raise HTTPException(status_code=409, detail="同名供应商已存在。")

                row = await repo.update(provider_id, data)
                if row is None:
                    raise HTTPException(status_code=404, detail="供应商不存在。")

                if clear_key:
                    row.api_key = None

                await _audit(
                    session,
                    identity=identity,
                    target_tenant="platform",
                    operation="model_provider_updated",
                    resource_type="model_provider",
                    resource_id=str(provider_id),
                    metadata=audit_meta,
                )

    invalidate_model_cache()
    return ProviderResponse.from_row(row)


@router.post("/model-providers/{provider_id}/activate", response_model=ProviderResponse)
async def activate_model_provider(
    provider_id: int,
    identity: Identity = Depends(require_platform_admin),
) -> ProviderResponse:
    """把某个供应商设为「使用中」。同一时刻只有一个生效。"""
    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            async with session.begin():
                row = await ModelProviderRepository(session).activate(provider_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="供应商不存在。")

                await _audit(
                    session,
                    identity=identity,
                    target_tenant="platform",
                    operation="model_provider_activated",
                    resource_type="model_provider",
                    resource_id=str(provider_id),
                    metadata={"name": row.name, "model": row.default_text_model},
                )

    invalidate_model_cache()
    return ProviderResponse.from_row(row)


@router.delete("/model-providers/{provider_id}")
async def delete_model_provider(
    provider_id: int,
    identity: Identity = Depends(require_platform_admin),
) -> dict:
    """删除供应商。

    如果删的是当前生效的那个，自动把剩余最早的一条设为生效，
    否则平台会在用户不知情的情况下退回 .env 的模型。
    """
    with platform_admin_tenant_scope("platform"):
        async with async_session_factory() as session:
            async with session.begin():
                repo = ModelProviderRepository(session)
                row = await repo.get(provider_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="供应商不存在。")

                was_active = bool(row.is_active)
                name = row.name
                await repo.delete(provider_id)

                promoted = None
                if was_active:
                    remaining = await repo.list_all()
                    if remaining:
                        promoted = (await repo.activate(remaining[0].id)).name

                await _audit(
                    session,
                    identity=identity,
                    target_tenant="platform",
                    operation="model_provider_deleted",
                    resource_type="model_provider",
                    resource_id=str(provider_id),
                    metadata={"name": name, "promoted": promoted},
                )

    invalidate_model_cache()
    return {
        "deleted": True,
        "promoted": promoted,
        "message": f"已删除 {name}；" + (f"{promoted} 已设为使用中。" if promoted else "当前退回 .env 配置。"),
    }


# 探测供应商要快：15 秒的无响应等待只会让人以为页面卡死。
# 连接单独设更短的超时，因为「连不上」和「连上了不回话」是两种完全不同的故障。
PROVIDER_DISCOVER_CONNECT_TIMEOUT = 5.0
PROVIDER_DISCOVER_READ_TIMEOUT = 20.0


def _is_private_endpoint(endpoint: str) -> bool:
    """判断是不是内网地址，用来补一句「谁访问不到谁」的提示。"""
    host = urlsplit(endpoint).hostname or ""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _is_loopback_endpoint(endpoint: str) -> bool:
    """回环地址：基本只有「反向隧道 / 本地起服务」这一种来源。"""
    host = urlsplit(endpoint).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _describe_provider_error(exc: Exception, endpoint: str) -> str:
    """把 httpx 异常翻成能照着办的一句话。

    原先直接 f"无法访问供应商：{exc}"，而 httpx 的 ConnectTimeout 不带 message，
    格式化出来是空串，页面只剩「无法访问供应商：」——超时、拒绝连接、DNS 失败
    在用户看来是同一个错误，只能靠猜。
    """
    if isinstance(exc, httpx.ConnectTimeout):
        head = (
            f"连接超时：{endpoint} 在 {PROVIDER_DISCOVER_CONNECT_TIMEOUT:g} 秒内"
            "没有建立连接"
        )
    elif isinstance(exc, httpx.ReadTimeout):
        head = (
            f"读取超时：已连上 {endpoint}，但 {PROVIDER_DISCOVER_READ_TIMEOUT:g} 秒内"
            "没有返回数据"
        )
    elif isinstance(exc, httpx.ConnectError):
        reason = str(exc) or "连接被拒绝或无路由"
        head = f"无法建立连接：{endpoint}（{reason}）"
    else:
        detail = str(exc) or "(无附加信息)"
        head = f"请求失败（{type(exc).__name__}）：{endpoint}（{detail}）"

    if _is_loopback_endpoint(endpoint):
        head += (
            "。这是回环地址，请求由平台服务自己发出：如果是靠反向隧道接的内网服务，"
            "先确认隧道还在（ss -ltnp | grep 端口），或在服务器上直接 curl 一次。"
        )
    elif _is_private_endpoint(endpoint):
        head += (
            "。该地址是内网地址，只有「发起请求的机器」——也就是平台服务本身——"
            "能到达才有效，浏览器能打开不代表服务器能打开；"
            "平台部署在公网而模型在内网时，需要先做端口转发或反向隧道。"
        )
    return head


def _describe_provider_status(status: int, body: str, endpoint: str) -> str:
    """供应商回了非 200：把状态码和它的原话都带上。"""
    head = f"供应商返回 {status}：{(body or '').strip()[:300] or '(空响应)'}"
    if status in (401, 403):
        head += "。鉴权没通过：核对 API Key，或确认网关要求的是 Bearer 还是 Basic。"
    elif status == 404:
        head += f"。接口路径可能不对：确认 Base URL 是否应包含 /v1（当前探测 {endpoint}）。"
    return head


@router.post("/model-providers/discover", response_model=DiscoverModelsResponse)
async def discover_models(
    payload: DiscoverModelsRequest,
    identity: Identity = Depends(require_platform_admin),
) -> DiscoverModelsResponse:
    """向供应商查询可用模型（GET {base_url}/models）。

    只读探测，不落库、不改配置；用于避免手抄模型名抄错——
    抄错的代价是对话直接 400，而且错误信息埋在日志里。
    """
    base_url = (payload.base_url or "").strip().rstrip("/")
    api_key = payload.api_key or None

    if payload.provider_id is not None:
        with platform_admin_tenant_scope("platform"):
            async with async_session_factory() as session:
                row = await ModelProviderRepository(session).get(payload.provider_id)
        if row is not None:
            base_url = base_url or (row.base_url or "").rstrip("/")
            api_key = api_key or row.api_key

    if not base_url:
        raise HTTPException(status_code=400, detail="需要 Base URL 才能查询模型列表。")

    endpoint = base_url if base_url.endswith("/models") else f"{base_url}/models"

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(
        PROVIDER_DISCOVER_READ_TIMEOUT, connect=PROVIDER_DISCOVER_CONNECT_TIMEOUT
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(endpoint, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "discover_models failed: endpoint=%s error=%s: %s",
            endpoint,
            type(exc).__name__,
            exc or "(空)",
        )
        raise HTTPException(
            status_code=400, detail=_describe_provider_error(exc, endpoint)
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=_describe_provider_status(response.status_code, response.text, endpoint),
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="供应商返回的不是合法 JSON。") from exc

    items = data.get("data") if isinstance(data, dict) else data
    models = [
        str(item.get("id"))
        for item in (items or [])
        if isinstance(item, dict) and item.get("id")
    ]

    return DiscoverModelsResponse(
        models=models,
        message=f"共发现 {len(models)} 个模型。" if models else "供应商未返回任何模型。",
    )


# ---------------- 远端 MCP ----------------

# 目前只支持 Streamable HTTP 一种传输：平台没有 stdio 进程托管，
# 也不打算把「在 API 进程里起子进程」这种能力开放给配置面。
MCP_TRANSPORT = "streamable-http"


def _registered_remote_map() -> dict[str, str]:
    """远端工具名 → 本地工具名。只有登记过的才会出现在模型目录里。

    映射的权威来源是 mcp_tools 里的 `_REMOTE_BY_LOCAL`。代码白名单现在为空
    （prv_ 那批下线，改由 FioCloud 连接器提供 fc_*），所以这张表通常也是空的：
    「已接入」由 `mcp_registered_tools` 决定。保留这个入口是为了以后再把工具
    登记回代码时，发现弹窗仍能认出「这个远端工具已经在代码里接过了」。
    """
    return remote_to_local_map()


async def _registered_count() -> int:
    """模型目录里来自 MCP 的工具数 = 代码白名单 + 管理员接入项。

    只数代码白名单会让「接入一个工具后这个数不变」，看着像没生效。
    """
    async with async_session_factory() as session:
        extra = await session.scalar(select(func.count()).select_from(McpRegisteredTool))

    return len(_registered_remote_map()) + int(extra or 0)


@router.get("/mcp", response_model=McpRuntimeResponse)
async def get_mcp_runtime(
    identity: Identity = Depends(require_platform_admin),
) -> McpRuntimeResponse:
    """当前 MCP 运行时配置。

    默认端点只读：它来自进程环境变量，改它要改 .env 并重启。
    其它端点跟着接入项存在库里（见 /mcp/tools），不需要重启。
    """
    tokens = settings.mcp_tenant_tokens or {}

    return McpRuntimeResponse(
        enabled=bool(settings.mcp_enabled),
        transport=MCP_TRANSPORT,
        server_url=settings.mcp_server_url,
        connect_timeout_seconds=float(settings.mcp_connect_timeout_seconds),
        max_result_chars=int(settings.mcp_max_result_chars),
        # 令牌只回「有没有、配给了哪些租户」，绝不回显内容。
        token_configured=bool(tokens),
        token_tenants=sorted(tokens.keys()),
        registered_count=await _registered_count(),
    )


@router.post("/mcp/discover", response_model=McpDiscoverResponse)
async def discover_mcp_tools(
    payload: McpDiscoverRequest,
    identity: Identity = Depends(require_platform_admin),
) -> McpDiscoverResponse:
    """拉取远端工具清单，并标出哪些已经接入本地白名单。

    发现 ≠ 接入：远端有什么，不等于模型能看到什么。未登记的工具照样
    不会出现在任何人的工具目录里，这里只是让管理员看清楚差在哪几个。

    安全边界：这个接口会拿管理员给的地址和令牌发起外连，属于 SSRF 面。
    约束有三条——必须 platform_admin、只允许 http(s)（McpToolClient 校验）、
    令牌不写日志也不进审计元数据。
    """
    url = (payload.url or settings.mcp_server_url or "").strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail="需要 MCP Server URL（.env 里的 MCP_SERVER_URL 未配置）。",
        )

    # 代码白名单只跟默认端点说话：拿它去标另一个服务上的同名工具，
    # 会给出一个「已接入」的假结论（模型其实调的是 .env 那台）。
    same_as_default = endpoint_key(url) == endpoint_key(settings.mcp_server_url)
    mapping = _registered_remote_map() if same_as_default else {}

    # 「发现」和「接入」必须用同一套取清单逻辑，否则会出现
    # 页面上看得见、点接入却说远端没有这个工具。
    try:
        by_name = await fetch_remote_tools(
            url=url,
            token=(payload.token or "").strip(),
            timeout_seconds=payload.timeout_seconds,
        )
    except McpConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except McpError as exc:
        raise HTTPException(status_code=400, detail=f"无法取得工具清单：{exc}") from exc

    descriptors = list(by_name.values())

    # 哪些是管理员在控制台接入的（可取消）。代码白名单里的不算。
    async with async_session_factory() as session:
        admin_map = await admin_registered_map(session, server_url=url)

    # 代码白名单优先：同一个远端工具两边都登记时，模型看到的是代码那份定义。
    for remote_name, local_name in admin_map.items():
        mapping.setdefault(remote_name, local_name)

    tools = [
        McpRemoteTool(
            name=item.name,
            description=(item.description or "").strip(),
            local_name=mapping.get(item.name),
            # 只有「模型看到的那份就是管理员接入的」才允许取消。
            # 代码白名单同名时以代码为准，那种情况不给按钮。
            # 先判非空：未接入时两侧都是 None，写等号会让它们全部变成可取消。
            removable=bool(admin_map.get(item.name))
            and admin_map.get(item.name) == mapping.get(item.name),
        )
        for item in descriptors
    ]
    registered = sum(1 for item in tools if item.local_name)

    logger.info(
        "管理员探测 MCP 工具清单。",
        extra={
            "event": "mcp_tools_discovered",
            "server": url,
            "count": len(tools),
            "registered": registered,
        },
    )

    return McpDiscoverResponse(
        tools=tools,
        count=len(tools),
        registered_count=registered,
        server_url=url,
        message=(
            f"远端共 {len(tools)} 个工具，其中 {registered} 个已接入。"
            if tools
            else "远端没有返回任何工具。"
        ),
    )


# 接入项是平台级的（MCP 通道只有一条），审计里的目标租户写成 platform。
_MCP_SCOPE = "platform"


async def _mcp_tool_responses(
    rows: list[McpRegisteredTool],
    *,
    gateway,
    tenant_id: str | None,
) -> list[McpRegisteredToolResponse]:
    """接入项 → 响应，顺带说清「令牌从哪来」。

    经连接器接入的行上 `token` 永远是空的：端点取 `connectors.base_url`、
    凭据按租户现取（`connector_service` 里"连接器完全接管"那条）。只回
    `token_set` 会把设计如此显示成「未配置」，而它和「连接器停用 / 本租户
    没配凭据」长得一模一样——后者是真的调不通。

    凭据状态按**调用方所在租户**（`identity.tenant_id`）算，与连接器列表页的
    `credential_set` 同一口径：同一份列表，A 看到已配置、B 看到未配置，这是对的。

    `tenant_id` 必须由调用方传，不能读上下文：`require_platform_admin` 会把
    上下文租户固定成平台租户。凭据表还挂着 RLS——事务开始时按上下文租户
    `set_config('app.tenant_id', ...)`，所以照上下文查会**一行都读不到**，
    而 RLS 的拒绝和「没配凭据」在代码里长得一模一样，界面就成了「全都没配」。
    读凭据要显式以该租户身份进入作用域。
    """
    connector_ids = {row.connector_id for row in rows if row.connector_id}
    connectors: dict[int, Connector] = {}
    credential_ids: set[int] = set()

    if connector_ids:
        async with async_session_factory() as session:
            found = (
                await session.scalars(select(Connector).where(Connector.id.in_(connector_ids)))
            ).all()
            connectors = {item.id: item for item in found}

        if tenant_id:
            with platform_admin_tenant_scope(tenant_id):
                async with async_session_factory() as session:
                    credential_rows = (
                        await session.scalars(
                            select(ConnectorCredential).where(
                                ConnectorCredential.tenant_id == tenant_id
                            )
                        )
                    ).all()
                    credential_ids = {item.connector_id for item in credential_rows}

    return [
        row_to_response(
            row,
            gateway=gateway,
            connector=connectors.get(row.connector_id) if row.connector_id else None,
            credential_set=(
                row.connector_id in credential_ids
                if row.connector_id and tenant_id
                else None
            ),
        )
        for row in rows
    ]


@router.get("/mcp/tools", response_model=McpRegisteredToolListResponse)
async def list_registered_mcp_tools(
    identity: Identity = Depends(require_platform_admin),
) -> McpRegisteredToolListResponse:
    """管理员接入过的 MCP 工具。代码白名单不在这个列表里（它不归配置管）。"""
    gateway = get_tool_gateway()

    async with async_session_factory() as session:
        rows = (
            await session.scalars(select(McpRegisteredTool).order_by(McpRegisteredTool.id))
        ).all()

    return McpRegisteredToolListResponse(
        items=await _mcp_tool_responses(
            list(rows), gateway=gateway, tenant_id=identity.tenant_id
        )
    )


@router.patch("/mcp/tools/{local_name}", response_model=McpRegisteredToolResponse)
async def update_registered_mcp_tool(
    local_name: str,
    payload: McpToolUpdateRequest,
    identity: Identity = Depends(require_platform_admin),
) -> McpRegisteredToolResponse:
    """改接入项的风险等级与展示文案。

    接入是批量做的（连接器「发现工具」一次接进几十个），那时只能给一个统一的
    保守值，于是「查一次压力」「列一下站点」也全成了 `write`——每问一句都弹
    审批单，平台等于不可用。风险等级得能按工具逐个调回来。

    审批由风险级**推导**（表里不存 `requires_approval`），所以这里必须同时
    改注册表里的那份定义：只改库会导致界面显示「读取」而实际照样弹审批单，
    两边不一致时人只会相信自己看到的那份。

    代码白名单里的同名工具不许改：那份风险级写在代码里，是要改代码的事。
    """
    if local_name in code_local_names():
        raise HTTPException(
            status_code=400,
            detail=f"{local_name} 由代码注册，风险等级写在代码里，不经由此接口修改。",
        )

    data = payload.model_dump(exclude_unset=True)
    # `None` 表示「这次不动」，空串是「清空说明」，两者不能混为一谈。
    data = {key: value for key, value in data.items() if value is not None}
    if not data:
        raise HTTPException(status_code=400, detail="没有要修改的字段。")

    gateway = get_tool_gateway()

    with platform_admin_tenant_scope(_MCP_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(McpRegisteredTool).where(
                        McpRegisteredTool.local_name == local_name
                    )
                )
                if row is None:
                    raise HTTPException(status_code=404, detail="该工具没有接入记录。")

                before = {
                    "risk_level": row.risk_level.value,
                    "display_name": row.display_name,
                    "timeout_seconds": int(row.timeout_seconds or 60),
                }

                if "risk_level" in data:
                    row.risk_level = data["risk_level"]
                if "display_name" in data:
                    row.display_name = data["display_name"]
                if "description" in data:
                    row.description = data["description"]
                if "timeout_seconds" in data:
                    row.timeout_seconds = data["timeout_seconds"]
                await session.flush()

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_MCP_SCOPE,
                    operation="mcp_tool_updated",
                    resource_type="mcp_registered_tool",
                    resource_id=str(row.id),
                    metadata={
                        "local_name": row.local_name,
                        "before": before,
                        "after": {
                            "risk_level": row.risk_level.value,
                            "display_name": row.display_name,
                            "timeout_seconds": int(row.timeout_seconds or 60),
                        },
                    },
                )

    synced = _resync_mcp_definition(gateway, row)
    if not synced:
        # 没在注册表里（停用中或上次注册失败）：库里已是对的，重启/重新接入时生效。
        logger.warning(
            "接入项已更新，但它当前不在注册表里，本次只改了库。",
            extra={"event": "mcp_tool_update_not_registered", "tool_name": row.local_name},
        )

    return (
        await _mcp_tool_responses([row], gateway=gateway, tenant_id=identity.tenant_id)
    )[0]


def _resync_mcp_definition(gateway, row: McpRegisteredTool) -> bool:
    """把库里的改动同步到注册表里那份定义。

    用 `replace` 而不是重新 `register_row`：后者要再拉一次远端 schema，
    而这次改动只涉及风险级与展示文案，不该为了改一个枚举值去联网。
    """
    if not gateway.has_tool(row.local_name):
        return False

    existing = gateway.get_tool(row.local_name)
    if existing.source != "mcp":
        return False

    gateway.register_tool(
        replace(
            existing,
            display_name=row.display_name,
            description=row.description or "",
            risk_level=row.risk_level,
            # 审批是「风险级 + 显式声明」的或；只换风险级会被旧的
            # requires_approval=True 顶回来，表现成「降成读取了还是要审批」。
            requires_approval=is_write_risk(row.risk_level),
            timeout_seconds=int(row.timeout_seconds or 60),
        ),
        replace=True,
    )
    return True


@router.post("/mcp/tools", response_model=McpRegisteredToolResponse, status_code=201)
async def register_mcp_tool(
    payload: McpToolRegisterRequest,
    identity: Identity = Depends(require_platform_admin),
) -> McpRegisteredToolResponse:
    """把远端的一个工具接入本地白名单。

    和「新建 HTTP Tool」的区别：这里没有空白新建，远端必须真的存在
    这个工具——入参 schema 直接来自远端，凭空写一份本地 schema 等于
    承诺一个自己都不确定的接口。

    校验必须发生在**这个工具将来真正调用的那个端点**上。早先这里固定用
    .env 的地址去查，于是在第二个 MCP 服务上发现到的工具，接入时一律
    报「远端没有这个工具」——工具明明在，只是查错了服务。
    """
    gateway = get_tool_gateway()

    try:
        ensure_name_allowed(payload.local_name, gateway=gateway)
    except McpToolNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    endpoint = (payload.server_url or settings.mcp_server_url or "").strip()
    if not endpoint:
        raise HTTPException(
            status_code=400,
            detail="需要 MCP Server URL（.env 里的 MCP_SERVER_URL 未配置）。",
        )

    try:
        by_name = await fetch_remote_tools(url=endpoint, token=payload.token or "")
    except McpConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except McpError as exc:
        raise HTTPException(status_code=400, detail=f"无法取得工具清单：{exc}") from exc

    descriptor = by_name.get(payload.remote_name)
    if descriptor is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{endpoint} 上没有名为 {payload.remote_name} 的工具。"
                "清单可能已变化，请重新「发现 tools」后再接入。"
            ),
        )

    # 默认端点的令牌按租户从 .env 取，不往库里抄一份；
    # 只有外部端点才存它自己的凭据（没有就当该端点不鉴权）。
    external_endpoint = endpoint_key(endpoint) != endpoint_key(settings.mcp_server_url)
    endpoint_token = ((payload.token or "").strip() or None) if external_endpoint else None

    with platform_admin_tenant_scope(_MCP_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                exists = await session.scalar(
                    select(McpRegisteredTool).where(
                        McpRegisteredTool.local_name == payload.local_name
                    )
                )
                if exists is not None:
                    raise HTTPException(status_code=409, detail="同名工具已接入。")

                row = McpRegisteredTool(
                    local_name=payload.local_name,
                    remote_name=payload.remote_name,
                    display_name=payload.display_name,
                    description=payload.description,
                    risk_level=payload.risk_level,
                    timeout_seconds=payload.timeout_seconds,
                    # 显式存下实际端点：以后再改 .env 的 MCP_SERVER_URL，
                    # 这条接入项不会跟着悄悄改指向。
                    server_url=endpoint,
                    token=endpoint_token,
                    enabled=True,
                    created_by=identity.subject,
                )
                session.add(row)
                await session.flush()

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_MCP_SCOPE,
                    operation="mcp_tool_registered",
                    resource_type="mcp_registered_tool",
                    resource_id=str(row.id),
                    metadata={
                        "local_name": row.local_name,
                        "remote_name": row.remote_name,
                        "risk_level": row.risk_level.value,
                        # 地址进审计（它不是秘密），令牌不进。
                        "server_url": row.server_url,
                        "token_set": bool(row.token),
                    },
                )

    if not register_row(gateway, row, descriptor):
        # 已落库但注册失败：这条记录留着，重启时会重试一次，
        # 状态在列表里显示为「未注册」，比悄悄回滚更难查。
        logger.error(
            "MCP 工具已接入但注册失败。",
            extra={"event": "mcp_registered_tool_failed", "tool_name": row.local_name},
        )

    return (
        await _mcp_tool_responses([row], gateway=gateway, tenant_id=identity.tenant_id)
    )[0]


@router.delete("/mcp/tools/{local_name}")
async def unregister_mcp_tool(
    local_name: str,
    identity: Identity = Depends(require_platform_admin),
) -> dict:
    """取消接入：库里删行，注册表同步摘掉。

    代码白名单里的工具不能走这里——它们的下线方式是改代码，
    在控制台点一下就能让审批路径上的工具消失，这个口子不能开。
    """
    if local_name in code_local_names():
        raise HTTPException(
            status_code=400,
            detail=f"{local_name} 由代码注册，不能在控制台取消接入。",
        )

    gateway = get_tool_gateway()

    with platform_admin_tenant_scope(_MCP_SCOPE):
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(McpRegisteredTool).where(
                        McpRegisteredTool.local_name == local_name
                    )
                )
                if row is None:
                    raise HTTPException(status_code=404, detail="该工具没有接入记录。")

                await session.delete(row)

                await _audit(
                    session,
                    identity=identity,
                    target_tenant=_MCP_SCOPE,
                    operation="mcp_tool_unregistered",
                    resource_type="mcp_registered_tool",
                    resource_id=str(row.id),
                    metadata={
                        "local_name": row.local_name,
                        "remote_name": row.remote_name,
                    },
                )

    unregister_row(gateway, local_name)

    return {"deleted": True, "local_name": local_name}
