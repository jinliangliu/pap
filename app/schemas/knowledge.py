# app/schemas/knowledge.py
"""知识库读写接口的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import APIModel


class KnowledgeDocumentItem(APIModel):
    id: str
    # 跨次入库稳定的逻辑标识。前端只做展示与调试用，不要拿它当主键。
    logical_id: str
    title: str
    filename: str | None = None
    source_uri: str
    # file = 目录扫描；upload = 页面上传。两者的生命周期互不影响。
    source_type: str = "file"
    mime_type: str | None = None
    size_bytes: int | None = None
    status: str = "ready"
    error_message: str | None = None
    access_scope: str = "internal"
    visibility: str = "tenant_shared"
    chunk_count: int = 0
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None = None
    # 原文还在不在。不在就没法重建，只能重新上传——列表里要能看出来，
    # 否则用户点「重建」只会拿到一个失败。
    reindexable: bool = True


class KnowledgeDocumentListResponse(APIModel):
    items: list[KnowledgeDocumentItem] = Field(default_factory=list)
    total: int = 0
    can_write: bool = False
    # 页面要显示「传什么样的文件」，这些值由后端给，避免前后端各写一份白名单。
    allowed_suffixes: list[str] = Field(default_factory=list)
    max_upload_bytes: int = 0
    upload_dir: str = ""
    directory: str = ""
    # 每条来源各有多少篇，页面顶部据此显示「目录 5 · 上传 2」。
    by_source: dict[str, int] = Field(default_factory=dict)


class KnowledgeChunkItem(APIModel):
    chunk_id: str
    chunk_index: int
    section: str = ""
    content: str
    is_active: bool = True


class KnowledgeDocumentDetailResponse(APIModel):
    document: KnowledgeDocumentItem
    chunks: list[KnowledgeChunkItem] = Field(default_factory=list)
    chunk_total: int = 0


class KnowledgeSyncResponse(APIModel):
    """目录同步的结果。"""

    documents: int = 0
    chunks: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    # 切片器升级后，跟随目录同步一起重切的上传文档数。它们不在目录里，
    # 不列出来会让「同步过了」看起来像全部迁移完成。
    migrated: int = 0
    skipped: list[str] = Field(default_factory=list)
    message: str = ""
