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
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import Field
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
