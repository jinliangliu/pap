# app/knowledge/store.py
"""知识切片在 PostgreSQL 中的唯一读写入口。

运行时检索只读 `documents` / `document_chunks`，本地 JSON 不再是运行时索引：
多进程各自持有本地文件时，重新入库后各进程看到的版本会分叉，
而数据库里的切片是唯一事实来源。

索引指纹是「该租户的索引版本号」，由入库流程显式自增；
TTL 由 `KNOWLEDGE_INDEX_REFRESH_SECONDS` 控制，避免每次检索都做一次聚合查询。

多租户：本模块的每个函数都显式进入 `tenant_scope`。
调用方即使忘了包上下文，写入也会被自动打上正确的 tenant_id，
而不会出现「有租户参数、但行里是空租户」的脏数据。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeIndexState,
)
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

# 固定命名空间：同一份文档在多次入库之间得到同一个 UUID，
# 切片 ID 因此保持稳定，引用不会因为重新入库而失效。
DOCUMENT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

DEFAULT_ACCESS_SCOPE = "internal"
DEFAULT_VISIBILITY = "tenant_shared"


def document_uuid(tenant_id: str, logical_id: str) -> uuid.UUID:
    """文档 UUID 必须带租户维度。

    `documents.id` 是全局主键，只按 logical_id 生成会让两个租户的同一份资料
    撞主键——第二家租户入库直接 IntegrityError。带上 tenant_id 后，
    同一租户重复入库仍然得到稳定 ID，跨租户互不干扰。
    """
    return uuid.uuid5(DOCUMENT_NAMESPACE, f"{tenant_id}:{logical_id}")


@dataclass(frozen=True)
class StoreStats:
    documents: int
    chunks: int
    version: str


def _hash_value(record: dict[str, Any]) -> str:
    """content_hash 列是 64 位，去掉 `sha256:` 前缀以适配。"""
    raw = str(record.get("content_hash") or "")
    return raw.removeprefix("sha256:")[:64] or "unknown"


def _acl(record: dict[str, Any]) -> dict[str, Any]:
    """切片的访问控制元数据。空 allowed_roles = 租户内可见。"""
    return {
        "access_scope": record.get("access_scope", DEFAULT_ACCESS_SCOPE),
        "visibility": record.get("visibility", DEFAULT_VISIBILITY),
        "allowed_roles": list(record.get("allowed_roles") or []),
        "owner_user_id": record.get("owner_user_id"),
    }


async def fingerprint(session: AsyncSession, tenant_id: str) -> str:
    """
    索引指纹 = 该租户的索引版本号。

    版本号由入库流程显式自增，比「数量 + 最近写入时间」可靠：
    同一秒内重复入库、或内容替换但切片数不变时也能区分。
    未入库过的租户返回 v0。
    """
    with tenant_scope(tenant_id):
        version = await session.scalar(
            select(KnowledgeIndexState.version).where(
                KnowledgeIndexState.tenant_id == tenant_id
            )
        )

    return f"v{int(version or 0)}"


async def bump_version(session: AsyncSession, tenant_id: str) -> int:
    """入库后自增版本号，唤醒所有实例的检索缓存。"""
    with tenant_scope(tenant_id):
        state = await session.get(KnowledgeIndexState, tenant_id)

        if state is None:
            state = KnowledgeIndexState(tenant_id=tenant_id, version=1)
            session.add(state)
        else:
            state.version = int(state.version) + 1

        await session.flush()

        return int(state.version)


async def stats(session: AsyncSession, tenant_id: str) -> StoreStats:
    with tenant_scope(tenant_id):
        statement = select(func.count(DocumentChunk.id)).where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.is_active.is_(True),
        )
        chunks = int((await session.scalar(statement)) or 0)

        documents_statement = select(func.count(Document.id)).where(
            Document.tenant_id == tenant_id,
            Document.status == DocumentStatus.READY,
        )
        documents = int((await session.scalar(documents_statement)) or 0)

        version = await fingerprint(session, tenant_id)

    return StoreStats(
        documents=documents,
        chunks=chunks,
        version=version,
    )


async def load_records(
    session: AsyncSession,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """读出全部可用切片，形状与旧 JSON 索引一致，检索层无需改动。"""
    with tenant_scope(tenant_id):
        statement = (
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.is_active.is_(True),
                Document.status == DocumentStatus.READY,
            )
            .order_by(Document.source_uri, DocumentChunk.chunk_index)
        )

        rows = (await session.execute(statement)).all()

    records: list[dict[str, Any]] = []

    for chunk, document in rows:
        meta = chunk.metadata_json or {}

        records.append(
            {
                "chunk_id": meta.get("chunk_id") or str(chunk.id),
                "document_id": meta.get("document_id") or str(chunk.document_id),
                "chunk_index": chunk.chunk_index,
                "text": chunk.content,
                "title": document.title,
                "section": meta.get("section", ""),
                "source_path": document.source_uri,
                "updated_at": meta.get("updated_at", ""),
                "content_hash": f"sha256:{document.content_hash}",
                "access_scope": meta.get("access_scope", DEFAULT_ACCESS_SCOPE),
                "visibility": meta.get("visibility", DEFAULT_VISIBILITY),
                "allowed_roles": meta.get("allowed_roles", []),
                "owner_user_id": meta.get("owner_user_id"),
                "document_type": meta.get("document_type", "knowledge_document"),
                "tags": meta.get("tags", []),
            }
        )

    return records


async def replace_all(
    session: AsyncSession,
    tenant_id: str,
    records: list[dict[str, Any]],
) -> StoreStats:
    """
    整库替换某个租户的知识切片。

    入库是「全量重建」语义（与旧的 JSON 覆盖一致），因此先删后插；
    整个过程在调用方的事务里完成，中途失败不会留下半个索引。
    """
    with tenant_scope(tenant_id):
        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id)
        )
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))

        grouped: dict[str, list[dict[str, Any]]] = {}

        for record in records:
            grouped.setdefault(str(record.get("document_id") or ""), []).append(record)

        for logical_id, chunks in grouped.items():
            if not logical_id:
                continue

            first = chunks[0]
            document_id = document_uuid(tenant_id, logical_id)

            document = Document(
                id=document_id,
                tenant_id=tenant_id,
                created_by="ingest",
                title=str(first.get("title") or logical_id),
                filename=str(first.get("source_path") or "").split("/")[-1] or None,
                source_uri=str(first.get("source_path") or logical_id),
                source_type="file",
                content_hash=_hash_value(first),
                content_size_bytes=None,
                status=DocumentStatus.READY,
                metadata_json={"document_id": logical_id},
            )

            session.add(document)

            for chunk in chunks:
                session.add(
                    DocumentChunk(
                        id=uuid.uuid5(document_id, f"chunk:{chunk.get('chunk_index', 0)}"),
                        document_id=document_id,
                        tenant_id=tenant_id,
                        chunk_index=int(chunk.get("chunk_index", 0)),
                        content=str(chunk.get("text") or ""),
                        token_count=None,
                        vector_id=None,
                        metadata_json={
                            "chunk_id": chunk.get("chunk_id"),
                            "document_id": logical_id,
                            "section": chunk.get("section", ""),
                            "updated_at": chunk.get("updated_at", ""),
                            "document_type": chunk.get(
                                "document_type", "knowledge_document"
                            ),
                            "tags": chunk.get("tags", []),
                            **_acl(chunk),
                        },
                        is_active=True,
                    )
                )

        await session.flush()
        await bump_version(session, tenant_id)

        status = await stats(session, tenant_id)

    logger.info(
        "知识切片已写入数据库。",
        extra={
            "event": "knowledge_store_written",
            "tenant_id": tenant_id,
            "documents": status.documents,
            "chunks": status.chunks,
        },
    )

    return status
