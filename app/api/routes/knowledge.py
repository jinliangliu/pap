# app/api/routes/knowledge.py
"""知识库接口：状态、文档管理、检索验证。

权限分两层，和技能一致：

- 读：任何成员都能看列表与详情，但只看到自己 `allowed_scopes` 覆盖的那部分
  （看不到的文档按 404 处理，不泄露「有一份你看不到的文档」）；
- 写（上传 / 重建 / 删除）：要 `knowledge:write` 或平台管理员。
  上传高分级文档还要额外持有对应 scope——否则任何能上传的人都能把一份
  机密资料标成 `internal`，等于自己给自己降级。

写操作落 ADMIN_ACTION 审计，只记文件名、大小与分级，不记正文。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import get_session
from app.knowledge import documents, store
from app.knowledge.index import get_knowledge_index
from app.knowledge.ingest import (
    CHUNKER_VERSION,
    SUPPORTED_SUFFIXES,
    IngestReport,
    KnowledgeIngestError,
    scan_directory,
)
from app.knowledge.retriever import allowed_scopes_for
from app.knowledge.upload import (
    WRITE_PERMISSION,
    UploadDenied,
    UploadRejected,
    ingest_upload,
    precheck_upload,
)
from app.models.audit import AuditEventType
from app.models.document import Document
from app.schemas.common import APIModel, as_utc
from app.schemas.knowledge import (
    KnowledgeChunkItem,
    KnowledgeDocumentDetailResponse,
    KnowledgeDocumentItem,
    KnowledgeDocumentListResponse,
    KnowledgeSyncResponse,
)
from app.services import audit_service
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

READ_PREFIX = "knowledge."


class KnowledgeStatusResponse(APIModel):
    indexed: bool
    chunks: int
    documents: int
    backend: str
    version: str
    knowledge_dir: str
    allowed_scopes: list[str]
    can_write: bool = False


class KnowledgeSearchRequest(APIModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=4, ge=1, le=10)


class KnowledgeSearchResponse(APIModel):
    found: bool
    answer: str
    citations: list[dict]
    # 语义重筛是否生效、剔除了几条、回落到 BM25 的原因。
    # 不显示这层信息，验证页就只能看到结果变少，查不出是模型判断还是故障。
    rerank: dict = Field(default_factory=dict)


def _can_write(identity: Identity) -> bool:
    return identity.is_platform_admin or WRITE_PERMISSION in identity.scopes


def _visible_scopes(identity: Identity) -> set[str]:
    return allowed_scopes_for(identity.scopes)


def _require_write(identity: Identity) -> None:
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail=f"需要 {WRITE_PERMISSION} 权限。")


def _item(identity: Identity, row: documents.DocumentRow) -> KnowledgeDocumentItem:
    return KnowledgeDocumentItem(
        id=row.id,
        logical_id=row.logical_id,
        title=row.title,
        filename=row.filename,
        source_uri=row.source_uri,
        source_type=row.source_type,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        status=row.status,
        error_message=row.error_message,
        access_scope=row.access_scope,
        visibility=row.visibility,
        chunk_count=row.chunk_count,
        created_by=row.created_by,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        indexed_at=as_utc(row.indexed_at),
        reindexable=documents.find_source_path(identity.tenant_id, row) is not None,
    )


async def _audit(
    session: AsyncSession,
    identity: Identity,
    *,
    operation: str,
    document_id: str,
    filename: str = "",
    size: int | None = None,
    access_scope: str = "",
    detail: str = "",
) -> None:
    await audit_service.write_audit_event(
        session,
        event_type=AuditEventType.ADMIN_ACTION,
        tenant_id=identity.tenant_id,
        actor_id=identity.user_id,
        resource_type="knowledge_document",
        resource_id=document_id,
        message=operation,
        metadata={
            "operation": operation,
            "actor_subject": identity.subject,
            "filename": filename,
            "size": size or 0,
            "access_scope": access_scope,
            "detail": detail,
        },
    )


def _invalidate(tenant_id: str) -> None:
    """写完立刻失效本进程缓存。

    指纹轮询是 5 秒一次，不主动清的话「刚上传就检索」会查不到，
    用户只会以为上传失败。
    """
    get_knowledge_index().invalidate(tenant_id)


# ---------------------------------------------------------------- 状态


@router.get("/status", response_model=KnowledgeStatusResponse)
async def knowledge_status(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeStatusResponse:
    """索引状态直接读库：进程内缓存只是检索用的派生结构，不是事实来源。"""
    stats = await store.stats(session, identity.tenant_id)
    retriever = await get_knowledge_index().get(
        session,
        identity.tenant_id,
        force=True,
    )

    return KnowledgeStatusResponse(
        indexed=retriever is not None and stats.chunks > 0,
        chunks=stats.chunks,
        documents=stats.documents,
        backend="postgres",
        version=stats.version,
        knowledge_dir=str(Path(settings.knowledge_dir)),
        allowed_scopes=sorted(_visible_scopes(identity)),
        can_write=_can_write(identity),
    )


# ---------------------------------------------------------------- 文档管理


@router.get("/documents", response_model=KnowledgeDocumentListResponse)
async def list_documents(
    include_deleted: bool = Query(default=False),
    source_type: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeDocumentListResponse:
    rows = await documents.list_documents(
        session,
        identity.tenant_id,
        include_deleted=include_deleted,
        source_type=source_type,
        limit=limit,
    )

    visible = _visible_scopes(identity)
    items = [_item(identity, row) for row in rows if row.access_scope in visible]
    by_source: dict[str, int] = {}

    for row in items:
        by_source[row.source_type] = by_source.get(row.source_type, 0) + 1

    return KnowledgeDocumentListResponse(
        items=items,
        total=len(items),
        can_write=_can_write(identity),
        allowed_suffixes=sorted(SUPPORTED_SUFFIXES),
        max_upload_bytes=settings.knowledge_max_file_bytes,
        upload_dir=str(Path(settings.knowledge_upload_dir)),
        directory=str(Path(settings.knowledge_dir)),
        by_source=by_source,
    )


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentDetailResponse)
async def get_document(
    document_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeDocumentDetailResponse:
    row = await documents.get_document(session, identity.tenant_id, document_id)

    # 分级不够时按「不存在」处理：403 会说漏「这里有一份你看不到的文档」。
    if row is None or row.access_scope not in _visible_scopes(identity):
        raise HTTPException(status_code=404, detail="文档不存在。")

    chunks = await documents.chunks_of(
        session,
        identity.tenant_id,
        document_id,
        limit=limit,
        offset=offset,
    )

    return KnowledgeDocumentDetailResponse(
        document=_item(identity, row),
        chunks=[
            KnowledgeChunkItem(
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.chunk_index,
                section=chunk.section,
                content=chunk.content,
                is_active=chunk.is_active,
            )
            for chunk in chunks
        ],
        chunk_total=row.chunk_count,
    )


@router.post("/documents", response_model=KnowledgeDocumentItem, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    access_scope: str | None = Form(default=None),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeDocumentItem:
    """上传一份文档并立即入库。

    这一层只做三件事：先校验请求（后缀、分级、权限），再把字节读全，
    最后把共享实现抛出的异常翻成 HTTP 状态码。校验与写入都在
    `app/knowledge/upload.py`——MCP 通道用的是同一份，两边不必各写一遍；
    各写一遍的下场是差别只出现在其中一条传输上，而那时最难判断。

    解析在落盘之前：解析失败不该在磁盘上留一个永远不会被索引的孤儿文件。
    """
    _require_write(identity)

    # 先校验再读 body：后缀不对时不该先把 5MB 读进内存才拒绝。
    try:
        precheck_upload(
            identity=identity,
            filename=file.filename or "",
            access_scope=access_scope,
        )
    except UploadDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    limit = settings.knowledge_max_file_bytes
    buffer = bytearray()

    while True:
        chunk = await file.read(1 << 20)

        if not chunk:
            break

        buffer.extend(chunk)

        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {limit // (1024 * 1024)} MB 上限。",
            )

    if not buffer:
        raise HTTPException(status_code=400, detail="文件是空的。")

    try:
        outcome = await ingest_upload(
            session=session,
            identity=identity,
            filename=file.filename or "",
            raw=bytes(buffer),
            title=title,
            access_scope=access_scope,
        )
    except UploadDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 写完之后读不回来：写入本身有问题，不是调用方能修的输入问题。
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _item(identity, outcome.row)


@router.delete("/documents/{document_id}", response_model=KnowledgeSyncResponse)
async def delete_document(
    document_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeSyncResponse:
    _require_write(identity)

    row = await documents.get_document(session, identity.tenant_id, document_id)

    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在。")

    if not await documents.soft_delete(session, identity.tenant_id, document_id):
        raise HTTPException(status_code=404, detail="文档不存在或已删除。")

    await _audit(
        session,
        identity,
        operation="delete",
        document_id=document_id,
        filename=row.filename or "",
        access_scope=row.access_scope,
    )
    await session.commit()
    _invalidate(identity.tenant_id)

    return KnowledgeSyncResponse(
        deleted=1,
        message=f"已下架《{row.title}》，{row.chunk_count} 个切片不再参与检索。",
    )


@router.post("/documents/{document_id}/reindex", response_model=KnowledgeDocumentItem)
async def reindex_document(
    document_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeDocumentItem:
    """按原文重切一份文档。

    原文丢了就标失败——不自动下架：一次解析失败不该让资料凭空消失，
    旧切片可能仍然可用。
    """
    _require_write(identity)

    row = await documents.get_document(session, identity.tenant_id, document_id)

    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在。")

    if row.deleted:
        raise HTTPException(status_code=409, detail="文档已下架，不能重建。")

    try:
        parsed = await asyncio.to_thread(
            documents.parse_for_reindex,
            identity.tenant_id,
            row,
        )
    except documents.DocumentError as exc:
        await documents.mark_failed(session, identity.tenant_id, document_id, str(exc))
        await _audit(
            session,
            identity,
            operation="reindex_failed",
            document_id=document_id,
            filename=row.filename or "",
            detail=str(exc)[:200],
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    with tenant_scope(identity.tenant_id):
        await documents.upsert_document(
            session,
            identity.tenant_id,
            parsed,
            actor=identity.user_id,
        )
        await store.bump_version(session, identity.tenant_id)
        await session.flush()
        await _audit(
            session,
            identity,
            operation="reindex",
            document_id=document_id,
            filename=row.filename or "",
            access_scope=parsed.acl["access_scope"],
        )
        updated = await documents.get_document(session, identity.tenant_id, document_id)
        await session.commit()

    _invalidate(identity.tenant_id)

    if updated is None:
        raise HTTPException(status_code=500, detail="重建后未能读回文档。")

    return _item(identity, updated)


@router.post("/reindex", response_model=KnowledgeSyncResponse)
async def sync_directory(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeSyncResponse:
    """把知识目录同步进库（增量）。

    只同步目录来源的文档：页面上传的文档不在目录里，按「目录里没有就删」
    的规则会把它们全部误杀。但切片器升级时上传文档同样需要重切，所以同步
    结束后会顺带把「仍按旧规则切过」的上传文档重建一遍——只重切内容，
    不增删文档，两条入口的存在性边界不变。
    """
    _require_write(identity)

    root = Path(settings.knowledge_dir)

    if not root.exists():
        raise HTTPException(status_code=400, detail=f"知识目录不存在：{root}")

    report = IngestReport()
    parsed = await asyncio.to_thread(scan_directory, root, report)

    with tenant_scope(identity.tenant_id):
        result = await documents.sync_directory(session, identity.tenant_id, parsed)
        migrated, migration_failed = await _migrate_outdated_uploads(
            session, identity.tenant_id, identity.user_id
        )

        if migrated:
            await store.bump_version(session, identity.tenant_id)

        # 上传文档重切会改变总切片数，统计要在迁移之后重新取。
        stats = await store.stats(session, identity.tenant_id)

        await _audit(
            session,
            identity,
            operation="sync",
            document_id="",
            detail=f"created={result.created} updated={result.updated} "
            f"unchanged={result.unchanged} deleted={result.deleted} migrated={migrated}",
        )
        await session.commit()

    _invalidate(identity.tenant_id)

    message = (
        f"新增 {result.created}、更新 {result.updated}、"
        f"跳过 {result.unchanged}、下架 {result.deleted}。"
    )

    if migrated:
        message += f" 另有 {migrated} 篇上传文档按新切片规则重建。"

    if migration_failed:
        message += f" {len(migration_failed)} 篇上传文档原文缺失，暂保留旧切片。"

    return KnowledgeSyncResponse(
        documents=stats.documents,
        chunks=stats.chunks,
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        deleted=result.deleted,
        migrated=migrated,
        skipped=report.skipped,
        message=message,
    )


async def _migrate_outdated_uploads(
    session: AsyncSession,
    tenant_id: str,
    actor: str,
) -> tuple[int, list[str]]:
    """重切仍按旧规则切过的上传文档，返回 (成功数, 失败文件名)。

    原文缺失时不标 failed：旧切片仍然可用，标失败只会让资料看起来消失。
    下次同步会再试，直到文件被补回来或文档被下架。
    """
    ids = await documents.outdated_upload_ids(session, tenant_id, chunker_version=CHUNKER_VERSION)
    migrated = 0
    failed: list[str] = []

    for document_id in ids:
        row = await documents.get_document(session, tenant_id, document_id)

        if row is None:
            continue

        try:
            parsed = await asyncio.to_thread(documents.parse_for_reindex, tenant_id, row)
        except (documents.DocumentError, KnowledgeIngestError) as exc:
            logger.warning(
                "上传文档无法按新规则重切，跳过切片器迁移。",
                extra={
                    "event": "knowledge_upload_migration_skipped",
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "reason": str(exc)[:200],
                },
            )
            failed.append(row.filename or document_id)
            continue

        await documents.upsert_document(session, tenant_id, parsed, actor=actor)
        migrated += 1

    return migrated, failed


# ---------------------------------------------------------------- 检索验证


class KnowledgeRecentQueryResponse(APIModel):
    """最近一次「对话里的知识库回答」真正送进检索的那句话。

    规划器会把用户原话做最小改写（去掉口语词、把代词换成上文对象）。
    改写本身是有意的，但用户看不到，于是同一个问题在对话里和在验证页里
    可能命中不同切片——排障的人会以为是检索坏了。这个接口把那一句交出来，
    验证页可以拿它复跑，对不上的时候一眼看出差在哪儿。
    """

    query: str
    asked_at: datetime | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None


@router.get("/recent-query", response_model=KnowledgeRecentQueryResponse | None)
async def knowledge_recent_query(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeRecentQueryResponse | None:
    """没有历史时返回 null，不是 404——「还没问过」不是错误。"""
    from app.models.conversation import Conversation, Message, MessageRole

    # 只取自己的对话：同租户成员能看同租户的文档，但「别人问过什么」
    # 不属于验证页需要知道的事。join 会话表才能按发起人收窄。
    row = (
        await session.execute(
            select(Message, Conversation.title)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.role == MessageRole.ASSISTANT)
            .where(Conversation.user_id == identity.user_id)
            .where(Message.metadata_json.op("->>")("knowledge_query").isnot(None))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).first()

    if row is None:
        return None

    message, title = row
    query = str((message.metadata_json or {}).get("knowledge_query") or "")

    if not query:
        return None

    return KnowledgeRecentQueryResponse(
        query=query,
        asked_at=as_utc(message.created_at),
        conversation_id=str(message.conversation_id) if message.conversation_id else None,
        conversation_title=title,
    )


@router.post("/search", response_model=KnowledgeSearchResponse)
async def knowledge_search(
    payload: KnowledgeSearchRequest,
    identity: Identity = Depends(get_current_identity),
) -> KnowledgeSearchResponse:
    from app.knowledge.service import answer_from_knowledge

    result = await answer_from_knowledge(
        query=payload.query,
        scopes=identity.scopes,
        tenant_id=identity.tenant_id,
        roles=identity.roles,
        user_id=identity.user_id,
        top_k=payload.limit,
    )

    return KnowledgeSearchResponse(
        found=result.found,
        answer=result.answer,
        citations=result.citations,
        rerank=result.rerank,
    )


# ---------------------------------------------------------------- 治理：加工流水线
#
# 下面这一组是「知识加工与治理」页面的数据源。全部要求 knowledge:write：
# 流水线指标本身不敏感，但同一组接口里还有反馈原文（用户当时问的什么）
# 与评测标注，把读权限放宽到全体成员，等于把手写事故记录公开在列表页。


class FeedbackRequest(APIModel):
    """点赞/踩。答案由前端带上，是因为写工具跑在另一条请求里，
    事后回查消息要多一次跨表读；这次反馈的现场就在这里，一次写入最省。"""

    verdict: str = Field(pattern="^(up|down)$")
    message_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    query: str = Field(default="", max_length=4000)
    answer: str = Field(default="", max_length=20000)
    reason: str | None = Field(default=None, max_length=32)
    comment: str | None = Field(default=None, max_length=2000)
    chunk_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)


class FeedbackHandledRequest(APIModel):
    handled: bool = True
    note: str | None = Field(default=None, max_length=1000)


class FeedbackToCaseRequest(APIModel):
    expect_document: list[str] = Field(default_factory=list)
    expect_section: str | None = Field(default=None, max_length=256)


class EvalCaseRequest(APIModel):
    id: str | None = None
    query: str = Field(min_length=1, max_length=4000)
    expect_found: bool = True
    expect_document: list[str] = Field(default_factory=list)
    expect_section: str | None = Field(default=None, max_length=256)
    category: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    enabled: bool = True
    source: str = "manual"


class EvalRunRequest(APIModel):
    k: int = Field(default=5, ge=1, le=10)
    # 只跑某一类。补完一类语料就只看这一类，比全量数字更能说明问题。
    category: str | None = None


class EvalImportRequest(APIModel):
    cases: list[dict] = Field(default_factory=list)


class RetireRequest(APIModel):
    document_ids: list[str] = Field(default_factory=list)


@router.get("/governance")
async def knowledge_governance(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """治理总览：八个阶段 + 满意度 + 最近一次评测 + 老化摘要。"""
    from app.knowledge import evaluation, feedback, lifecycle, pipeline

    _require_write(identity)

    stages = await pipeline.overview(session, identity.tenant_id)
    sat = await feedback.satisfaction(session, identity.tenant_id)
    life = await lifecycle.inspect(session, identity.tenant_id)
    runs = await _recent_runs(session, identity.tenant_id, limit=1)
    cases = await evaluation.list_cases(session, identity.tenant_id)

    return {
        **stages,
        "satisfaction": sat,
        "latest_eval": runs[0] if runs else None,
        "eval_cases": {
            "total": len(cases),
            "scored": sum(1 for item in cases if item["scored"]),
            "enabled": sum(1 for item in cases if item["enabled"]),
        },
        "lifecycle": life["summary"],
    }


async def _recent_runs(session: AsyncSession, tenant_id: str, *, limit: int = 20) -> list[dict]:
    from sqlalchemy import select

    from app.models.knowledge_gov import KnowledgeEvalRun
    from app.tenancy.context import tenant_scope

    with tenant_scope(tenant_id):
        rows = list(
            (
                await session.scalars(
                    select(KnowledgeEvalRun)
                    .where(KnowledgeEvalRun.tenant_id == tenant_id)
                    .order_by(KnowledgeEvalRun.created_at.desc())
                    .limit(max(1, limit))
                )
            ).all()
        )

    return [
        {
            "id": str(row.id),
            "k": row.k,
            "case_total": row.case_total,
            "scored": row.scored,
            "recall": row.recall,
            "precision": row.precision,
            "hit_at_k": row.hit_at_k,
            "mrr": row.mrr,
            "false_hit_rate": row.false_hit_rate,
            "zero_hit_rate": row.zero_hit_rate,
            "index_version": row.index_version,
            "elapsed_ms": row.elapsed_ms,
            "summary": row.summary,
            "created_by": row.created_by,
            "created_at": as_utc(row.created_at),
        }
        for row in rows
    ]


@router.get("/documents/{document_id}/pipeline")
async def knowledge_document_pipeline(
    document_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """单篇的加工明细。老文档没有入库快照时按现有切片现算，标 derived。"""
    from app.knowledge import pipeline

    _require_write(identity)
    row = await documents.get_document(session, identity.tenant_id, document_id)

    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在。")

    chunks = await documents.chunks_of(session, identity.tenant_id, document_id, limit=200)

    try:
        target = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="文档不存在。") from None

    with tenant_scope(identity.tenant_id):
        orm_row = await session.get(Document, target)

    if orm_row is None or orm_row.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="文档不存在。")

    payload = pipeline.document_detail(
        orm_row,
        [
            {"section": chunk.section, "content": chunk.content}
            for chunk in chunks
        ],
    )

    return {"document": _item(identity, row), **payload}


# ---------------------------------------------------------------- 治理：评测


@router.get("/eval/cases")
async def knowledge_eval_cases(
    enabled: bool | None = Query(default=None),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import evaluation

    _require_write(identity)
    items = await evaluation.list_cases(session, identity.tenant_id, enabled=enabled)

    return {
        "items": items,
        "total": len(items),
        "scored": sum(1 for item in items if item["scored"]),
    }


@router.post("/eval/cases")
async def knowledge_eval_case_save(
    payload: EvalCaseRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import evaluation

    _require_write(identity)

    try:
        item = await evaluation.upsert_case(
            session,
            identity.tenant_id,
            case_id=payload.id,
            query=payload.query,
            expect_found=payload.expect_found,
            expect_document=payload.expect_document,
            expect_section=payload.expect_section,
            category=payload.category,
            note=payload.note,
            enabled=payload.enabled,
            source=payload.source,
            actor=identity.user_id,
        )
    except evaluation.CaseRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()

    return item


@router.delete("/eval/cases/{case_id}")
async def knowledge_eval_case_delete(
    case_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import evaluation

    _require_write(identity)

    if not await evaluation.delete_case(session, identity.tenant_id, case_id):
        raise HTTPException(status_code=404, detail="用例不存在。")

    await session.commit()

    return {"deleted": 1}


@router.post("/eval/import")
async def knowledge_eval_import(
    payload: EvalImportRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import evaluation

    _require_write(identity)

    if not payload.cases:
        raise HTTPException(status_code=400, detail="cases 不能为空。")

    result = await evaluation.import_cases(
        session, identity.tenant_id, payload.cases, actor=identity.user_id
    )
    await session.commit()

    return result


@router.post("/eval/run")
async def knowledge_eval_run(
    payload: EvalRunRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """跑一次评测并落库。

    用当前在线索引、当前身份可见的分级——评的就是这个人实际能检索到的东西。
    """
    import time

    from app.knowledge import evaluation, store
    from app.models.knowledge_gov import KnowledgeEvalRun

    _require_write(identity)

    cases = await evaluation.list_cases(session, identity.tenant_id, enabled=True)

    if payload.category:
        cases = [item for item in cases if (item.get("category") or "") == payload.category]

    if not cases:
        raise HTTPException(
            status_code=400,
            detail="没有可跑的用例。先在「评估用例」里加几条，或从 bad case 转一条过来。",
        )

    started = time.monotonic()

    try:
        summary, results = await evaluation.run_online(
            session,
            identity.tenant_id,
            cases,
            k=payload.k,
            # 传原始授权而不是展开后的分级：评测内部要复用线上那条
            # `service._retrieve`，它自己会把 scope 折成可见分级。
            # 把已展开的集合再折一次会得到空集，评测就变成「什么都看不到」。
            scopes=identity.scopes,
            roles=tuple(identity.roles),
            user_id=identity.user_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    elapsed = int((time.monotonic() - started) * 1000)
    stats = await store.stats(session, identity.tenant_id)

    row = KnowledgeEvalRun(
        tenant_id=identity.tenant_id,
        k=payload.k,
        case_total=summary["cases"],
        scored=summary["scored"],
        recall=summary["recall"],
        precision=summary["precision"],
        hit_at_k=summary["hit_at_k"],
        mrr=summary["mrr"],
        false_hit_rate=summary["false_hit_rate"],
        zero_hit_rate=summary["zero_hit_rate"],
        index_version=stats.version,
        elapsed_ms=elapsed,
        summary=summary,
        detail=[item.to_dict() for item in results],
        created_by=identity.user_id,
    )
    session.add(row)
    await session.commit()

    return {
        "id": str(row.id),
        "metrics": summary,
        "index_version": stats.version,
        "elapsed_ms": elapsed,
        "detail": [item.to_dict() for item in results],
    }


@router.get("/eval/runs")
async def knowledge_eval_runs(
    limit: int = Query(default=20, ge=1, le=100),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require_write(identity)
    runs = await _recent_runs(session, identity.tenant_id, limit=limit)

    return {"items": runs, "total": len(runs)}


@router.get("/eval/runs/{run_id}")
async def knowledge_eval_run_detail(
    run_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.models.knowledge_gov import KnowledgeEvalRun

    _require_write(identity)

    try:
        target = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="评测记录不存在。") from None

    with tenant_scope(identity.tenant_id):
        row = await session.get(KnowledgeEvalRun, target)

    if row is None or row.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="评测记录不存在。")

    return {
        "id": str(row.id),
        "k": row.k,
        "summary": row.summary,
        "index_version": row.index_version,
        "elapsed_ms": row.elapsed_ms,
        "created_at": as_utc(row.created_at),
        "detail": row.detail,
    }


# ---------------------------------------------------------------- 治理：反馈


@router.post("/feedback")
async def knowledge_feedback_submit(
    payload: FeedbackRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """应用端的点赞/踩。

    **不要求 knowledge:write**：这是使用者的动作，不是管理员操作。
    能提问的人就能评价回答——把入口挡在权限后面，就永远收不到 bad case。
    """
    from app.knowledge import feedback

    try:
        item = await feedback.record(
            session,
            identity.tenant_id,
            message_id=payload.message_id,
            conversation_id=payload.conversation_id,
            run_id=payload.run_id,
            query=payload.query,
            answer=payload.answer,
            verdict=payload.verdict,
            reason=payload.reason,
            comment=payload.comment,
            chunk_ids=payload.chunk_ids,
            document_ids=payload.document_ids,
            actor=identity.user_id,
        )
    except feedback.FeedbackRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()

    return item


@router.get("/feedback")
async def knowledge_feedback_list(
    verdict: str | None = Query(default=None),
    handled: bool | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import feedback

    _require_write(identity)

    return await feedback.list_feedback(
        session, identity.tenant_id, verdict=verdict, handled=handled, limit=limit
    )


@router.post("/feedback/{feedback_id}/handled")
async def knowledge_feedback_handled(
    feedback_id: str,
    payload: FeedbackHandledRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import feedback

    _require_write(identity)

    item = await feedback.mark_handled(
        session, identity.tenant_id, feedback_id, handled=payload.handled, note=payload.note
    )

    if item is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")

    await session.commit()

    return item


@router.post("/feedback/{feedback_id}/case")
async def knowledge_feedback_to_case(
    feedback_id: str,
    payload: FeedbackToCaseRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """把差评升格成评测用例——这是反馈闭环真正闭合的那一步。"""
    from app.knowledge import feedback

    _require_write(identity)

    item = await feedback.promote_to_case(
        session,
        identity.tenant_id,
        feedback_id,
        expect_document=payload.expect_document,
        expect_section=payload.expect_section,
        actor=identity.user_id,
    )

    if item is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")

    await session.commit()

    return item


# ---------------------------------------------------------------- 治理：生命周期


@router.get("/lifecycle")
async def knowledge_lifecycle(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.knowledge import lifecycle

    _require_write(identity)

    return await lifecycle.inspect(session, identity.tenant_id)


@router.post("/lifecycle/retire")
async def knowledge_lifecycle_retire(
    payload: RetireRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """批量下架。必须显式给出 id —— 没有「一键清理全部候选」。"""
    from app.knowledge import lifecycle

    _require_write(identity)

    if not payload.document_ids:
        raise HTTPException(status_code=400, detail="document_ids 不能为空。")

    if len(payload.document_ids) > 50:
        raise HTTPException(status_code=400, detail="一次最多下架 50 份，分批处理。")

    result = await lifecycle.retire(session, identity.tenant_id, payload.document_ids)

    for document_id in result["retired"]:
        await _audit(
            session,
            identity,
            operation="retire",
            document_id=document_id,
            detail="生命周期治理：批量下架",
        )

    await session.commit()
    _invalidate(identity.tenant_id)

    return {
        **result,
        "message": f"已下架 {len(result['retired'])} 份，未找到 {len(result['missing'])} 份。",
    }
