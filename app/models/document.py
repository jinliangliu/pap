# app/models/document.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TenantScoped


class DocumentStatus(str, enum.Enum):
    """文档摄取和索引状态。"""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class Document(TenantScoped, Base):
    """
    知识库原始文档元数据。

    原始文件应由对象存储、本地持久化卷或文档系统保存。
    source_uri 只保存逻辑引用或相对路径，不应保存 Secret URL。
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index(
            "ix_documents_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_documents_tenant_source_hash",
            "tenant_id",
            "content_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    created_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    filename: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    source_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="file",
        server_default="file",
    )
    mime_type: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    content_size_bytes: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=DocumentStatus.PENDING,
        # native_enum=False 的 Enum 持久化成员名，默认值必须与之一致：
        # 写 `.value`（"pending"）时，只有从不显式赋 status 的插入才会踩到
        # LookupError，而那条路径平时没人走，坏了也发现不了。
        server_default=DocumentStatus.PENDING.name,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    metadata_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )


class DocumentChunk(TenantScoped, Base):
    """
    文档切片及检索元数据。

    embedding 数据通常由 Chroma、pgvector 或专用向量数据库管理。
    本模型保留 vector_id，用于关联外部向量条目。

    如决定使用 PostgreSQL + pgvector，可在后续迁移和模型中将
    embedding 替换为 pgvector.sqlalchemy.Vector(dimension) 字段。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index(
            "ix_document_chunks_document_chunk_index",
            "document_id",
            "chunk_index",
            unique=True,
        ),
        Index(
            "ix_document_chunks_tenant_document",
            "tenant_id",
            "document_id",
        ),
        Index(
            "ix_document_chunks_vector_id",
            "vector_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    token_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    vector_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        unique=True,
    )

    metadata_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    document: Mapped[Document] = relationship(
        back_populates="chunks",
    )


class KnowledgeIndexState(Base):
    """
    知识索引版本号。

    每个进程在内存里缓存一份 BM25 结构，用这个版本号判断是否需要重建：
    入库时自增，各实例看到版本变化后重新从数据库加载切片。
    比「数量 + 最近写入时间」可靠——同一秒内重复入库也能区分。
    """

    __tablename__ = "knowledge_index_state"
    __tenant_scoped__ = True

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
