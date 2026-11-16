# app/knowledge/documents.py
"""知识文档的增量读写与生命周期。

**为什么不再全量替换**

早期入库是「先删光该租户所有文档，再插入扫描结果」。只有目录扫描时这没问题，
接上页面上传之后就出事：一次目录同步会把页面上传的文档全部删掉。

现在的模型是每份文档独立 upsert：

- 内容哈希相同且切片数一致 → 跳过，不重建（`unchanged`）；
- 变了 → 删该文档的切片、重插（只影响这一份）；
- 目录扫描只同步 `source_type='file'` 的文档，上传的（`'upload'`）由上传接口管。

**软删而不是硬删**

`status` 置 `deleted`、切片置 `is_active=False`、`deleted_at` 记时间。
检索层本来就只读 `status='ready'` 的切片，所以下架立即生效；
保留行是为了「谁在什么时候删了什么」有据可查，也避免误删后无从恢复。
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.knowledge import store
from app.knowledge.ingest import (
    SOURCE_DIRECTORY,
    SOURCE_UPLOAD,
    ParsedDocument,
    build_document,
    extract_text_from_bytes,
    parse_front_matter,
)
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)


class DocumentError(RuntimeError):
    """文档操作失败（找不到、文件丢失、内容无法解析）。"""


@dataclass(frozen=True)
class DocumentRow:
    """列表用的一行。脱离 ORM 对象，避免出事务后懒加载。"""

    id: str
    # 跨次入库稳定的逻辑标识：重建时必须沿用它，换了它等于新建一份文档，
    # 引用与命中记录会全部指向旧的那份。
    logical_id: str
    title: str
    filename: str | None
    source_uri: str
    source_type: str
    mime_type: str | None
    size_bytes: int | None
    status: str
    error_message: str | None
    access_scope: str
    visibility: str
    chunk_count: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None

    @property
    def deleted(self) -> bool:
        return self.status == DocumentStatus.DELETED.value


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: str
    chunk_index: int
    section: str
    content: str
    is_active: bool


@dataclass(frozen=True)
class SyncResult:
    documents: int
    chunks: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


def _chunker_version_of(metadata: dict[str, Any] | None) -> int:
    """老行没写版本号按 1 处理；脏值也按 1，别让同步接口 500。"""
    try:
        return int((metadata or {}).get("chunker_version") or 1)
    except (TypeError, ValueError):
        return 1


def _status_name(raw: Any) -> str:
    """对外的状态值统一用小写枚举值（`ready` / `deleted`）。

    `native_enum=False` 的 Enum 在库里持久化的是成员名（`READY`），
    前端拿到大写再自己转一层很容易漏，所以出口统一成 value。
    """
    if isinstance(raw, DocumentStatus):
        return str(raw.value)
    return str(raw or "")


def acl_of(row: Document) -> dict[str, Any]:
    """文档级 ACL。公开给引用解析用：一份文档能不能被某个 Agent 看，
    必须和列表/检索走同一个判定，不能各写一套。"""
    meta = row.metadata_json or {}
    acl = meta.get("acl")

    if isinstance(acl, dict):
        return acl

    # 历史数据：ACL 只存在切片里，文档级没留副本。它们的 scope 全是 internal。
    return {"access_scope": "internal", "visibility": "tenant_shared"}


def _row_of(row: Document, chunk_count: int) -> DocumentRow:
    meta = row.metadata_json or {}

    return DocumentRow(
        id=str(row.id),
        logical_id=str(meta.get("document_id") or row.id),
        title=row.title,
        filename=row.filename,
        source_uri=row.source_uri,
        source_type=row.source_type,
        mime_type=row.mime_type,
        size_bytes=row.content_size_bytes,
        status=_status_name(row.status),
        error_message=row.error_message,
        access_scope=str(acl_of(row).get("access_scope") or "internal"),
        visibility=str(acl_of(row).get("visibility") or "tenant_shared"),
        chunk_count=chunk_count,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        indexed_at=row.indexed_at,
    )


async def _chunk_count(session: AsyncSession, document_id: uuid.UUID) -> int:
    return int(
        (
            await session.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.is_active.is_(True),
                )
            )
        )
        or 0
    )


# ---------------------------------------------------------------- 写入


async def upsert_document(
    session: AsyncSession,
    tenant_id: str,
    document: ParsedDocument,
    *,
    actor: str | None = None,
) -> str:
    """写入一份文档，返回 `created` / `updated` / `unchanged`。

    调用方负责事务与 `bump_version`——批量同步时每份都 bump 一次，
    会让其他实例在同步过程中反复重建索引。
    """
    document_id = store.document_uuid(tenant_id, document.logical_id)
    raw_hash = document.content_hash.removeprefix("sha256:")[:64]

    with tenant_scope(tenant_id):
        existing = await session.get(Document, document_id)
        current_chunks = 0

        if existing is not None:
            current_chunks = await _chunk_count(session, document_id)
            existing_meta = existing.metadata_json or {}
            # 老行没有这个字段，按 1 处理——升级后第一次同步必须重建，
            # 否则「第 N 段」永远不会变成真实小节标题。
            existing_version = _chunker_version_of(existing_meta)

            if (
                existing.status == DocumentStatus.READY
                and existing.content_hash == raw_hash
                and current_chunks == document.chunk_count
                and existing_version == document.chunker_version
            ):
                return "unchanged"

        now = datetime.now(UTC)
        action = "created" if existing is None else "updated"

        row = existing or Document(
            id=document_id,
            tenant_id=tenant_id,
            created_at=now,
        )

        row.title = document.title
        row.filename = document.filename
        row.source_uri = document.source_uri
        row.source_type = document.source_type
        row.mime_type = mimetypes.guess_type(document.filename or document.source_uri)[0]
        row.content_hash = raw_hash
        row.content_size_bytes = document.size_bytes
        row.status = DocumentStatus.READY
        row.error_message = None
        row.indexed_at = now
        # 时间字段是 server_default：异步会话里读它要回一次库，
        # 插入后立刻访问会 MissingGreenlet，所以显式赋值。
        row.updated_at = now
        row.deleted_at = None
        row.created_by = row.created_by or actor or "api"
        row.metadata_json = {
            "document_id": document.logical_id,
            "acl": document.acl,
            "updated_at": document.updated_at,
            "chunker_version": document.chunker_version,
        }

        if existing is None:
            session.add(row)

        await session.flush()

        # 先删后插：切片主键由 document_id + 序号派生，同一事务里
        # 不 flush 掉删除，重插同主键会撞。
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        await session.flush()

        for index, chunk in enumerate(document.chunks):
            session.add(
                DocumentChunk(
                    id=uuid.uuid5(document_id, f"chunk:{index}"),
                    document_id=document_id,
                    tenant_id=tenant_id,
                    chunk_index=index,
                    content=chunk,
                    token_count=None,
                    vector_id=None,
                    metadata_json={
                        "chunk_id": f"{document.logical_id}-{index:04d}",
                        "document_id": document.logical_id,
                        "section": (
                            document.sections[index]
                            if index < len(document.sections)
                            else f"第 {index + 1} 段"
                        ),
                        "updated_at": document.updated_at,
                        "document_type": "knowledge_document",
                        "tags": [],
                        **document.acl,
                    },
                    is_active=True,
                )
            )

        await session.flush()

    return action


async def sync_directory(
    session: AsyncSession,
    tenant_id: str,
    documents: list[ParsedDocument],
) -> SyncResult:
    """把扫描结果同步进库：新增、更新、跳过，并下架目录里已消失的文档。"""
    seen: set[uuid.UUID] = set()
    created = updated = unchanged = 0
    chunks = 0

    for document in documents:
        seen.add(store.document_uuid(tenant_id, document.logical_id))
        action = await upsert_document(session, tenant_id, document)

        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        else:
            unchanged += 1

        if action != "unchanged":
            chunks += document.chunk_count

    stale = await _stale_directory_documents(session, tenant_id, seen)
    deleted = 0

    for row in stale:
        await _mark_deleted(session, row)
        deleted += 1

    if created or updated or deleted:
        await store.bump_version(session, tenant_id)
        await session.flush()

    stats = await store.stats(session, tenant_id)

    logger.info(
        "知识目录已同步。",
        # 键名不能撞 LogRecord 的保留字段：`created` 是日志记录时间戳，
        # 用它会抛 KeyError("Attempt to overwrite 'created' in LogRecord")，
        # 接口直接 500。测试默认 WARNING 级别不会构造 record，所以只有
        # 生产日志级别下才炸——这类坑必须靠「把级别调上来」的测试钉住。
        extra={
            "event": "knowledge_directory_synced",
            "tenant_id": tenant_id,
            "created_documents": created,
            "updated_documents": updated,
            "unchanged_documents": unchanged,
            "deleted_documents": deleted,
        },
    )

    return SyncResult(
        documents=stats.documents,
        chunks=stats.chunks,
        created=created,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
    )


async def outdated_upload_ids(
    session: AsyncSession,
    tenant_id: str,
    *,
    chunker_version: int,
) -> list[str]:
    """挑出仍按旧切片规则切过的上传文档。

    目录同步只扫磁盘，碰不到上传文档——但切片器升级时它们同样要重切，
    否则会永远停在「第 N 段」。这里只挑、不改：重建交给调用方逐篇走
    `parse_for_reindex` + `upsert_document`，目录同步「不动上传文档的存在性」
    这条边界依然成立，变的只是切片内容。
    """
    with tenant_scope(tenant_id):
        rows = (
            (
                await session.execute(
                    select(Document).where(
                        Document.tenant_id == tenant_id,
                        Document.status == DocumentStatus.READY,
                        Document.source_type == SOURCE_UPLOAD,
                    )
                )
            )
            .scalars()
            .all()
        )

    return [
        str(row.id) for row in rows if _chunker_version_of(row.metadata_json) != chunker_version
    ]


async def _stale_directory_documents(
    session: AsyncSession,
    tenant_id: str,
    seen: set[uuid.UUID],
) -> list[Document]:
    """目录里已经没有的 file 类文档。

    只碰 `source_type='file'`：上传的文档不在目录里，按目录判断存亡
    会全部误杀。
    """
    with tenant_scope(tenant_id):
        statement = select(Document).where(
            Document.tenant_id == tenant_id,
            Document.source_type == SOURCE_DIRECTORY,
            Document.status != DocumentStatus.DELETED,
        )

        if seen:
            statement = statement.where(Document.id.not_in(seen))

        return list((await session.scalars(statement)).all())


async def _mark_deleted(session: AsyncSession, row: Document) -> None:
    now = datetime.now(UTC)
    row.status = DocumentStatus.DELETED
    row.deleted_at = now
    row.updated_at = now

    await session.execute(
        update(DocumentChunk).where(DocumentChunk.document_id == row.id).values(is_active=False)
    )


async def soft_delete(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
) -> bool:
    """下架一份文档。检索层只读 `status='ready'`，所以删除立即生效。"""
    try:
        target = uuid.UUID(document_id)
    except ValueError:
        return False

    with tenant_scope(tenant_id):
        row = await session.get(Document, target)

        if row is None or row.tenant_id != tenant_id or row.status == DocumentStatus.DELETED:
            return False

        await _mark_deleted(session, row)
        await store.bump_version(session, tenant_id)
        await session.flush()

    logger.info(
        "知识文档已下架。",
        extra={
            "event": "knowledge_document_deleted",
            "tenant_id": tenant_id,
            "document_id": document_id,
        },
    )

    return True


async def mark_failed(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
    message: str,
) -> None:
    """重建失败：保留文档与旧切片，只标状态。

    直接下架会让一次解析失败变成「资料凭空消失」，而旧切片可能仍然可用。
    等下一次成功重建时 `error_message` 会被清掉。
    """
    try:
        target = uuid.UUID(document_id)
    except ValueError:
        return

    with tenant_scope(tenant_id):
        row = await session.get(Document, target)

        if row is None or row.tenant_id != tenant_id:
            return

        row.status = DocumentStatus.FAILED
        row.error_message = message[:1000]
        row.updated_at = datetime.now(UTC)
        await session.flush()


# ---------------------------------------------------------------- 读取


async def list_documents(
    session: AsyncSession,
    tenant_id: str,
    *,
    include_deleted: bool = False,
    source_type: str | None = None,
    limit: int = 200,
) -> list[DocumentRow]:
    """列表带切片数：一次左连接聚合，不按行去数（N+1）。"""
    with tenant_scope(tenant_id):
        statement = (
            select(Document, func.count(DocumentChunk.id))
            .outerjoin(
                DocumentChunk,
                (DocumentChunk.document_id == Document.id) & (DocumentChunk.is_active.is_(True)),
            )
            .where(Document.tenant_id == tenant_id)
            .group_by(Document.id)
            .order_by(Document.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )

        if not include_deleted:
            statement = statement.where(Document.status != DocumentStatus.DELETED)

        if source_type:
            statement = statement.where(Document.source_type == source_type)

        rows = (await session.execute(statement)).all()

    return [_row_of(row, int(count or 0)) for row, count in rows]


async def get_document(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
    *,
    include_deleted: bool = True,
) -> DocumentRow | None:
    try:
        target = uuid.UUID(document_id)
    except ValueError:
        return None

    with tenant_scope(tenant_id):
        row = await session.get(Document, target)

        if row is None or row.tenant_id != tenant_id:
            return None

        if row.status == DocumentStatus.DELETED and not include_deleted:
            return None

        count = await _chunk_count(session, target)

    return _row_of(row, count)


async def find_upload_by_hash(
    session: AsyncSession,
    tenant_id: str,
    content_hash: str,
) -> DocumentRow | None:
    """按内容哈希找同租户已存在的上传文档，让重复上传变成幂等。

    只认 `status='ready'` 的：已下架或失败的不算「已存在」，否则重新上传
    一份之前删掉的资料会变成「什么也没发生」。
    """
    raw = content_hash.removeprefix("sha256:")[:64]

    with tenant_scope(tenant_id):
        row = await session.scalar(
            select(Document)
            .where(
                Document.tenant_id == tenant_id,
                Document.source_type == SOURCE_UPLOAD,
                Document.content_hash == raw,
                Document.status == DocumentStatus.READY,
            )
            .order_by(Document.created_at.desc())
        )

        if row is None:
            return None

        count = await _chunk_count(session, row.id)

    return _row_of(row, count)


async def chunks_of(
    session: AsyncSession,
    tenant_id: str,
    document_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[ChunkRow]:
    try:
        target = uuid.UUID(document_id)
    except ValueError:
        return []

    with tenant_scope(tenant_id):
        statement = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == target,
                DocumentChunk.tenant_id == tenant_id,
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
        )

        rows = (await session.scalars(statement)).all()

    return [
        ChunkRow(
            chunk_id=str((row.metadata_json or {}).get("chunk_id") or row.id),
            chunk_index=row.chunk_index,
            section=str((row.metadata_json or {}).get("section") or ""),
            content=row.content,
            is_active=bool(row.is_active),
        )
        for row in rows
    ]


# ---------------------------------------------------------------- 文件位置


def upload_dir(tenant_id: str) -> Path:
    return Path(settings.knowledge_upload_dir) / tenant_id


def store_upload_file(
    *,
    tenant_id: str,
    document_id: str,
    filename: str,
    raw: bytes,
) -> Path:
    """把上传的原文落盘，文件名用文档 id。

    不用用户给的文件名：那是不可信输入，`../` 与同名覆盖都要处理。
    原始文件名单独存在 `documents.filename` 里，展示时用它。
    """
    suffix = Path(filename).suffix.lower()
    directory = upload_dir(tenant_id)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{document_id}{suffix}"
    path.write_bytes(raw)

    return path


def find_source_path(tenant_id: str, row: DocumentRow) -> Path | None:
    """按文档来源定位原文，用于重建。找不到返回 None。"""
    if row.source_type == SOURCE_UPLOAD:
        suffix = Path(row.filename or "").suffix.lower()
        candidate = upload_dir(tenant_id) / f"{row.id}{suffix}"
    else:
        candidate = Path(settings.knowledge_dir) / row.source_uri

    return candidate if candidate.is_file() else None


def parse_for_reindex(tenant_id: str, row: DocumentRow) -> ParsedDocument:
    """从原文重新解析一份已存在的文档。"""
    path = find_source_path(tenant_id, row)

    if path is None:
        raise DocumentError("原文已不存在，无法重建。请重新上传。")

    raw = path.read_bytes()
    text = extract_text_from_bytes(raw, path.name)

    meta: dict[str, str] = {}

    if path.suffix.lower() in {".md", ".markdown", ".txt"}:
        meta, body = parse_front_matter(text)
        text = body

    return build_document(
        text,
        logical_id=row.logical_id,
        title=meta.get("title") or row.title,
        source_uri=row.source_uri,
        source_type=row.source_type,
        filename=row.filename,
        size_bytes=len(raw),
        meta=meta,
        acl_hint=row.source_uri,
    )
