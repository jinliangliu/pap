# app/api/routes/memories.py
"""长期记忆的读写接口。

权限模型与观测页同构，但更严一档：

- 任何成员都能读写**自己的**记忆（`user_id == 自己`）；
- 租户共享记忆（`user_id` 为空）只有平台管理员或持有 `memory:read` 的人能改；
- 别人的记忆**根本查不到**——不是 403 是 404。403 等于确认「这条存在」。

删除是物理删除，不留档：用户删掉的记忆不该还能从数据库里捞出来。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.memory import (
    Memory,
    MemoryReview,
    MemoryReviewStatus,
    MemoryStatus,
    MemoryType,
)
from app.schemas.memory import (
    MemoryCreateRequest,
    MemoryDreamConfig,
    MemoryDreamConfigUpdate,
    MemoryDreamRequest,
    MemoryDreamResponse,
    MemoryExtractionStats,
    MemoryItem,
    MemoryListResponse,
    MemoryReviewItem,
    MemoryReviewListResponse,
    MemoryUpdateRequest,
)
from app.services import memory_dream, memory_service
from app.services.audit_service import write_admin_action
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])

# 看/管整个租户的记忆。生产库里 dev 角色需要手动补这一行，见 docs/MEMORY.md。
READ_PERMISSION = "memory:read"


def _can_manage_tenant(identity: Identity) -> bool:
    return identity.is_platform_admin or READ_PERMISSION in identity.scopes


def _item(row: Memory) -> MemoryItem:
    return MemoryItem(
        id=row.id,
        memory_type=row.memory_type,
        title=row.title,
        content=row.content,
        source=row.source,
        confidence=row.confidence,
        is_user_confirmed=row.is_user_confirmed,
        scope="tenant" if row.user_id is None else "self",
        source_thread_id=row.source_thread_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


def _ensure_can_touch(row: Memory, identity: Identity) -> None:
    """不存在和没权限返回同一个结果。

    租户级记忆只允许管理员改；个人记忆只允许本人改。
    两种情况都抛 404，避免成为「这条 id 存在吗」的探针。
    """
    if row.user_id is None:
        if not _can_manage_tenant(identity):
            raise HTTPException(status_code=404, detail="记忆不存在。")
        return
    if row.user_id != identity.user_id:
        raise HTTPException(status_code=404, detail="记忆不存在。")


def _review_item(
    review: MemoryReview, memory: Memory | None, identity: Identity
) -> MemoryReviewItem:
    return MemoryReviewItem(
        id=review.id,
        memory_id=review.memory_id,
        # 原记忆可能已被删除，这时给空串让页面显示「原记忆已删除」，
        # 而不是让整个列表 500。
        memory_title=memory.title if memory else "",
        memory_content=memory.content if memory else "",
        action=review.action,
        status=review.status,
        reason=review.reason,
        proposed_title=review.proposed_title,
        proposed_content=review.proposed_content,
        merge_target_id=review.merge_target_id,
        evidence=list(review.evidence_json or []),
        confidence=review.confidence,
        model=review.model,
        prompt_version=review.prompt_version,
        created_at=review.created_at,
        resolved_at=review.resolved_at,
        can_apply=review.user_id == identity.user_id,
    )


@router.get("/reviews", response_model=MemoryReviewListResponse)
async def list_memory_reviews(
    status: str = Query(default="pending", pattern="^(pending|applied|dismissed|all)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(get_current_identity),
) -> MemoryReviewListResponse:
    """梦境建议列表。

    管理员可以只读查看全租户（用来判断抽取质量），但应用与忽略仍只允许本人：
    记忆是隐私，替别人确认等于替他改人格设定。
    """
    scope_user = None if _can_manage_tenant(identity) else identity.user_id
    target_status = None if status == "all" else MemoryReviewStatus(status)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows, total = await memory_dream.list_reviews(
                session,
                user_id=scope_user,
                status=target_status,
                limit=limit,
                offset=offset,
            )
            memories: dict = {}
            ids = [row.memory_id for row in rows]
            if ids:
                for memory in (
                    await session.scalars(select(Memory).where(Memory.id.in_(ids)))
                ).all():
                    memories[memory.id] = memory
            items = [_review_item(row, memories.get(row.memory_id), identity) for row in rows]

    return MemoryReviewListResponse(items=items, total=total)


@router.get("/extraction-stats", response_model=MemoryExtractionStats)
async def extraction_stats(
    days: int = Query(default=7, ge=1, le=90),
    identity: Identity = Depends(get_current_identity),
) -> MemoryExtractionStats:
    """自动抽取的确认率。口径的偏差写在响应 note 里，别当精确通过率用。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            stats = await memory_dream.extraction_stats(
                session, user_id=identity.user_id, days=days
            )

    return MemoryExtractionStats(
        days=stats.days,
        auto_total=stats.auto_total,
        auto_confirmed=stats.auto_confirmed,
        auto_pending=stats.auto_pending,
        auto_superseded=stats.auto_superseded,
        reviews_applied=stats.reviews_applied,
        confirmation_rate=stats.confirmation_rate,
    )


def _dream_config_item(config: memory_dream.DreamConfig, *, can_edit: bool) -> MemoryDreamConfig:
    return MemoryDreamConfig(**config.as_dict(), can_edit=can_edit)


@router.get("/dream/config", response_model=MemoryDreamConfig)
async def get_dream_config(
    identity: Identity = Depends(get_current_identity),
) -> MemoryDreamConfig:
    """读取本租户的梦境配置（已与平台默认合并）。

    任何成员都能读：读不到就不知道「开始梦境」按钮为什么点不动，
    也就没法解释给管理员听。写只有管理员可以。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            config = await memory_dream.resolve_dream_config(session, tenant_id=identity.tenant_id)

    return _dream_config_item(config, can_edit=_can_manage_tenant(identity))


@router.put("/dream/config", response_model=MemoryDreamConfig)
async def update_dream_config(
    payload: MemoryDreamConfigUpdate,
    identity: Identity = Depends(get_current_identity),
) -> MemoryDreamConfig:
    """写入本租户的梦境配置。

    改的是「这个租户想不想跑、跑多狠」，不影响别的租户；
    平台层的 `MEMORY_DREAM_ENABLED` 仍然优先——它关掉时这里保存成功也依然不可用。
    """
    if not _can_manage_tenant(identity):
        raise HTTPException(status_code=403, detail=f"缺少 {READ_PERMISSION} 权限。")

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            try:
                config = await memory_dream.save_dream_config(
                    session,
                    tenant_id=identity.tenant_id,
                    enabled=payload.enabled,
                    days=payload.days,
                    max_per_run=payload.max_per_run,
                    actor_user_id=identity.user_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            # 这个开关会改变「模型拿什么证据判断用户记忆」，出问题要能追到人。
            await write_admin_action(
                session,
                actor_id=identity.user_id,
                actor_subject=identity.subject,
                actor_tenant=identity.tenant_id,
                target_tenant=identity.tenant_id,
                operation="memory.dream_config.update",
                resource_type="tenant_memory_settings",
                resource_id=identity.tenant_id,
                metadata={
                    "enabled": payload.enabled,
                    "days": payload.days,
                    "max_per_run": payload.max_per_run,
                    "effective_enabled": config.enabled,
                    "effective_days": config.days,
                    "effective_max_per_run": config.max_per_run,
                },
            )
            await session.commit()

    return _dream_config_item(config, can_edit=True)


@router.post("/dream", response_model=MemoryDreamResponse)
async def dream_memories(
    payload: MemoryDreamRequest,
    identity: Identity = Depends(get_current_identity),
) -> MemoryDreamResponse:
    """手动触发一次梦境。

    刻意不做成每轮后台任务：一次梦境要为每条记忆调一次模型，
    放在对话链路上既拖延迟又浪费；按需触发、看结果再决定更合理。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            config = await memory_dream.resolve_dream_config(session, tenant_id=identity.tenant_id)

    if not config.enabled:
        raise HTTPException(
            status_code=400,
            detail=config.disabled_reason or "梦境已关闭。",
        )

    report = await memory_dream.run_dreaming(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        # 不传就跟着租户配置走，否则界面上的「回溯天数」永远不生效。
        days=payload.days if payload.days is not None else config.days,
        limit=config.clamp_limit(payload.limit),
        dry_run=payload.dry_run,
    )
    return MemoryDreamResponse(**report.as_dict())


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    status: str = Query(default="active", pattern="^(active|superseded|all)$"),
    memory_type: MemoryType | None = Query(default=None),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(get_current_identity),
) -> MemoryListResponse:
    """我的记忆 + 团队共享记忆。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows, total = await memory_service.list_memories(
                session,
                user_id=identity.user_id,
                status=None if status == "all" else MemoryStatus(status),
                memory_type=memory_type,
                query=q,
                limit=limit,
                offset=offset,
            )
            items = [_item(row) for row in rows]

    return MemoryListResponse(
        items=items,
        total=total,
        enabled=bool(settings.memory_enabled),
        can_manage_tenant=_can_manage_tenant(identity),
    )


@router.post("", response_model=MemoryItem, status_code=201)
async def create_memory(
    payload: MemoryCreateRequest,
    identity: Identity = Depends(get_current_identity),
) -> MemoryItem:
    if payload.scope == "tenant" and not _can_manage_tenant(identity):
        raise HTTPException(status_code=403, detail="没有管理团队共享记忆的权限。")

    owner = None if payload.scope == "tenant" else identity.user_id
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row, created = await memory_service.create_memory(
                    session,
                    user_id=owner,
                    memory_type=payload.memory_type,
                    title=payload.title,
                    content=payload.content,
                    source="user",
                    confidence=1.0,
                    is_user_confirmed=True,
                )
                if owner is not None:
                    await memory_service.enforce_cap(session, user_id=owner)
            item = _item(row)

    logger.info(
        "记忆写入。",
        extra={
            "event": "memory_written",
            # 键名不能叫 created：那是 LogRecord 的保留字段（时间戳），
            # logging 会直接抛 KeyError，接口变成 500，而数据其实已经提交了。
            "is_new": created,
            "memory_type": payload.memory_type.value,
            "scope": payload.scope,
        },
    )
    return item


@router.patch("/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdateRequest,
    identity: Identity = Depends(get_current_identity),
) -> MemoryItem:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await memory_service.get_memory(session, memory_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="记忆不存在。")
                _ensure_can_touch(row, identity)
                row = await memory_service.update_memory(
                    session,
                    row,
                    title=payload.title,
                    content=payload.content,
                    memory_type=payload.memory_type,
                    # 人手动改过的内容按「已确认」算，不再让它在页面上挂着「待确认」。
                    is_user_confirmed=True,
                )
            item = _item(row)
    return item


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await memory_service.get_memory(session, memory_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="记忆不存在。")
                _ensure_can_touch(row, identity)
                title = row.title
                await memory_service.delete_memory(session, row)

    # 审计只留「删了一条什么类型」，不留内容：删掉的东西不该在日志里复活。
    logger.info(
        "记忆删除。",
        extra={"event": "memory_deleted", "memory_id": str(memory_id), "title": title},
    )
    return {"deleted": True, "message": "已删除，无法恢复。"}


@router.post("/{memory_id}/confirm", response_model=MemoryItem)
async def confirm_memory(
    memory_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> MemoryItem:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await memory_service.get_memory(session, memory_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="记忆不存在。")
                _ensure_can_touch(row, identity)
                row = await memory_service.confirm_memory(session, row)
            item = _item(row)
    return item


@router.post("/reviews/{review_id}/apply", response_model=MemoryItem)
async def apply_memory_review(
    review_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> MemoryItem:
    """应用一条梦境建议。只允许记忆所属人执行。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                review = await session.get(MemoryReview, review_id)
                if review is None or review.user_id != identity.user_id:
                    raise HTTPException(status_code=404, detail="建议不存在。")
                try:
                    memory = await memory_dream.apply_review(
                        session, review, actor_user_id=identity.user_id
                    )
                except memory_dream.ReviewConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            item = _item(memory)
    return item


@router.post("/reviews/{review_id}/dismiss")
async def dismiss_memory_review(
    review_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    """忽略一条建议。记忆不动，建议留档。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                review = await session.get(MemoryReview, review_id)
                if review is None or review.user_id != identity.user_id:
                    raise HTTPException(status_code=404, detail="建议不存在。")
                try:
                    await memory_dream.dismiss_review(session, review)
                except memory_dream.ReviewConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"dismissed": True}
