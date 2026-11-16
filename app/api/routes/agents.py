# app/api/routes/agents.py
"""智能体管理接口。

读开放给所有成员：不知道有哪些智能体，就没法在对话时选；
写需要 `agent:write`——定义一个「谁来做事、能碰什么」是治理动作，不是日常操作。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.schemas.agent import (
    AgentCapabilitiesResponse,
    AgentCreateRequest,
    AgentItem,
    AgentListResponse,
    AgentStatusRequest,
    AgentUpdateRequest,
)
from app.services import agent_service
from app.services.audit_service import write_audit_event
from app.tenancy.context import tenant_scope
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

WRITE_PERMISSION = "agent:write"


def _can_manage(identity: Identity) -> bool:
    return identity.is_platform_admin or WRITE_PERMISSION in identity.scopes


def _require_manage(identity: Identity) -> None:
    if not _can_manage(identity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少 agent:write 权限，无法管理智能体。",
        )


def _item(row: Agent) -> AgentItem:
    return AgentItem(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        instruction=row.instruction,
        allowed_tools=row.allowed_tools,
        allowed_skills=row.allowed_skills,
        allowed_connectors=row.allowed_connectors,
        can_write=row.can_write,
        max_delegation_depth=row.max_delegation_depth,
        delegation_budget=row.delegation_budget,
        status=row.status.value,
        is_default=row.is_default,
        is_builtin=row.is_builtin,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
        usable=row.status == AgentStatus.ACTIVE,
    )


async def _load(session: AsyncSession, ref: str) -> Agent:
    try:
        return await agent_service.get_agent(session, ref=ref)
    except agent_service.AgentNotFound as exc:
        # 不存在与不属于本租户返回同一个结果，避免成为「这个 id 存在吗」的探针。
        raise HTTPException(status_code=404, detail="智能体不存在。") from exc


@router.get("", response_model=AgentListResponse)
async def list_agents(
    identity: Identity = Depends(get_current_identity),
) -> AgentListResponse:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows = list(await agent_service.list_agents(session))
            created = False

            if not rows:
                # 惰性补齐默认 Agent：新租户第一次打开页面就能看到一个可用选项，
                # 而不是「列表为空 + 对话却能跑」这种自相矛盾的状态。
                await agent_service.ensure_default(
                    session,
                    tenant_id=identity.tenant_id,
                    actor_user_id=identity.user_id,
                )
                created = True

            # 构建助手同期补齐，理由同上：第一次打开控制台就该能问它。
            # 失败不抛给调用方——一个助手起不来不该让整个智能体页打不开，
            # 但日志里必须留明确事件，否则「助手去哪了」无从查起。
            if not any(row.is_builtin for row in rows):
                try:
                    await agent_service.ensure_studio(
                        session,
                        tenant_id=identity.tenant_id,
                        actor_user_id=identity.user_id,
                    )
                    created = True
                except agent_service.AgentValidationError as exc:
                    logger.error(
                        "构建助手未能创建：%s",
                        exc,
                        extra={"event": "agent_studio_unavailable", "tenant_id": identity.tenant_id},
                    )
            elif await agent_service.sync_studio_definition(
                session, tenant_id=identity.tenant_id
            ):
                # 助手的工具清单由代码定义，老助手不会自动获得新工具。
                # 对齐时已经写过库，这里只要把列表重新读一遍。
                rows = list(await agent_service.list_agents(session))

            if created:
                await session.commit()
                rows = list(await agent_service.list_agents(session))

    return AgentListResponse(
        items=[_item(row) for row in rows],
        total=len(rows),
        can_manage=_can_manage(identity),
        counts=agent_service.status_counts(rows),
    )


@router.post("", response_model=AgentItem, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreateRequest,
    identity: Identity = Depends(get_current_identity),
) -> AgentItem:
    _require_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            try:
                row = await agent_service.create_agent(
                    session,
                    tenant_id=identity.tenant_id,
                    name=payload.name,
                    display_name=payload.display_name,
                    description=payload.description,
                    instruction=payload.instruction,
                    allowed_tools=payload.allowed_tools,
                    allowed_skills=payload.allowed_skills,
                    allowed_connectors=payload.allowed_connectors,
                    can_write=payload.can_write,
                    max_delegation_depth=payload.max_delegation_depth,
                    delegation_budget=payload.delegation_budget,
                    status=AgentStatus(payload.status),
                    actor_user_id=identity.user_id,
                )
            except agent_service.AgentValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            await write_audit_event(
                session,
                event_type="agent_created",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="agent",
                resource_id=str(row.id),
                metadata={
                    "name": row.name,
                    "status": row.status.value,
                    "can_write": row.can_write,
                    "allowed_tools": row.allowed_tools,
                    "max_delegation_depth": row.max_delegation_depth,
                    "delegation_budget": row.delegation_budget,
                },
            )
            # updated_at 由数据库 onupdate 生成，UPDATE 后该属性会被标记过期，
            # 提交后再读是一次隐式 IO——async 会话里会直接抛 MissingGreenlet。
            await session.refresh(row)
            await session.commit()
            return _item(row)


@router.get("/{ref}/capabilities", response_model=AgentCapabilitiesResponse)
async def get_agent_capabilities(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> AgentCapabilitiesResponse:
    """一个智能体「能做什么」，供界面展示与转交前的选择。

    读权限与清单一致（所有成员可读）：不知道对方能做什么，就没法决定把什么事交给它。

    **按调用者的可见性求交**，不是列出该 Agent 的全部能力——否则这个接口
    就变成了「探测别人有什么工具」的情报接口。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load(session, ref)

            # 只带身份，不带任何 Agent 边界：这里要回答的是「以我现在的身份，
            # 能看到对方的哪些能力」，不能被调用者自己的边界再裁一刀。
            context = ToolExecutionContext(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                thread_id="",
                run_id="",
                request_id="",
                roles=identity.roles,
                scopes=identity.scopes,
                is_platform_admin=identity.is_platform_admin,
            )

            data = await agent_service.capabilities_of(
                session, agent=row, context=context
            )

    return AgentCapabilitiesResponse(**data)


@router.get("/{ref}", response_model=AgentItem)
async def get_agent(
    ref: str,
    identity: Identity = Depends(get_current_identity),
) -> AgentItem:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            return _item(await _load(session, ref))


@router.patch("/{ref}", response_model=AgentItem)
async def update_agent(
    ref: str,
    payload: AgentUpdateRequest,
    identity: Identity = Depends(get_current_identity),
) -> AgentItem:
    _require_manage(identity)

    # 「没传」与「显式传 null」必须分开：前者保持原值，后者是明确取消收窄。
    # pydantic 用 model_fields_set 提供这个信息，不能用 `is not None` 判断。
    sent = payload.model_fields_set

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load(session, ref)
            before = {
                "allowed_tools": row.allowed_tools,
                "allowed_skills": row.allowed_skills,
                "allowed_connectors": row.allowed_connectors,
                "can_write": row.can_write,
                "max_delegation_depth": row.max_delegation_depth,
                "delegation_budget": row.delegation_budget,
                "revision": row.revision,
            }

            try:
                await agent_service.update_agent(
                    session,
                    agent=row,
                    display_name=payload.display_name,
                    description=payload.description,
                    instruction=payload.instruction,
                    allowed_tools=(
                        payload.allowed_tools if "allowed_tools" in sent else agent_service.UNSET
                    ),
                    allowed_skills=(
                        payload.allowed_skills if "allowed_skills" in sent else agent_service.UNSET
                    ),
                    allowed_connectors=(
                        payload.allowed_connectors
                        if "allowed_connectors" in sent
                        else agent_service.UNSET
                    ),
                    can_write=payload.can_write,
                    max_delegation_depth=(
                        payload.max_delegation_depth
                        if "max_delegation_depth" in sent
                        else agent_service.UNSET
                    ),
                    delegation_budget=(
                        payload.delegation_budget
                        if "delegation_budget" in sent
                        else agent_service.UNSET
                    ),
                    actor_user_id=identity.user_id,
                )
            except agent_service.AgentValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if row.revision != before["revision"]:
                await write_audit_event(
                    session,
                    event_type="agent_updated",
                    tenant_id=identity.tenant_id,
                    actor_id=identity.user_id,
                    resource_type="agent",
                    resource_id=str(row.id),
                    metadata={
                        "name": row.name,
                        # 只记变化前后，不记整段指令正文（可能很长，也可能含业务细节）。
                        "revision": row.revision,
                        "before": before,
                        "after": {
                            "allowed_tools": row.allowed_tools,
                            "allowed_skills": row.allowed_skills,
                            "allowed_connectors": row.allowed_connectors,
                            "can_write": row.can_write,
                            "max_delegation_depth": row.max_delegation_depth,
                            "delegation_budget": row.delegation_budget,
                            "revision": row.revision,
                        },
                    },
                )

            await session.refresh(row)
            await session.commit()
            return _item(row)


@router.post("/{ref}/status", response_model=AgentItem)
async def set_agent_status(
    ref: str,
    payload: AgentStatusRequest,
    identity: Identity = Depends(get_current_identity),
) -> AgentItem:
    _require_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load(session, ref)
            previous = row.status.value

            try:
                await agent_service.set_status(
                    session,
                    agent=row,
                    status=AgentStatus(payload.status),
                    actor_user_id=identity.user_id,
                )
            except agent_service.AgentValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if previous != row.status.value:
                await write_audit_event(
                    session,
                    event_type="agent_status_changed",
                    tenant_id=identity.tenant_id,
                    actor_id=identity.user_id,
                    resource_type="agent",
                    resource_id=str(row.id),
                    metadata={
                        "name": row.name,
                        "from": previous,
                        "to": row.status.value,
                    },
                )

            await session.refresh(row)
            await session.commit()
            return _item(row)


@router.delete("/{ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    ref: str,
    identity: Identity = Depends(get_current_identity),
):
    _require_manage(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load(session, ref)

            try:
                await agent_service.delete_agent(session, agent=row)
            except agent_service.AgentValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            await write_audit_event(
                session,
                event_type="agent_deleted",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="agent",
                resource_id=str(row.id),
                metadata={"name": row.name},
            )
            await session.commit()

    return None
