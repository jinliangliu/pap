# app/knowledge/index.py
"""按索引指纹热加载的检索索引。

每个进程在内存里保留一份 BM25 结构（分词后的切片），但事实来源是数据库：
检索前比对指纹，指纹变化就重建，因此重新入库后所有实例都会在下一次检索时
看到新内容，不需要重启，也不会出现「有的实例新、有的实例旧」。

指纹查询按 `KNOWLEDGE_INDEX_REFRESH_SECONDS` 节流，检索本身仍然零 DB 往返。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.knowledge import store
from app.knowledge.retriever import KnowledgeRetriever

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexedCorpus:
    version: str
    retriever: KnowledgeRetriever
    chunks: int


class KnowledgeIndex:
    def __init__(self) -> None:
        self._corpora: dict[str, IndexedCorpus] = {}
        self._checked_at: dict[str, float] = {}

    async def get(
        self,
        session: AsyncSession,
        tenant_id: str,
        *,
        force: bool = False,
    ) -> KnowledgeRetriever | None:
        """返回当前租户的检索器；索引为空时返回 None（调用方给出「资料不足」）。"""
        corpus = self._corpora.get(tenant_id)
        now = time.monotonic()
        checked = self._checked_at.get(tenant_id, 0.0)

        if (
            corpus is not None
            and not force
            and now - checked < settings.knowledge_index_refresh_seconds
        ):
            return corpus.retriever

        version = await store.fingerprint(session, tenant_id)
        self._checked_at[tenant_id] = now

        if corpus is not None and corpus.version == version and not force:
            return corpus.retriever

        records = await store.load_records(session, tenant_id)
        retriever = KnowledgeRetriever(records)

        self._corpora[tenant_id] = IndexedCorpus(
            version=version,
            retriever=retriever,
            chunks=len(records),
        )

        logger.info(
            "知识索引已加载。",
            extra={
                "event": "knowledge_index_loaded",
                "tenant_id": tenant_id,
                "chunks": len(records),
                "version": version[:64],
            },
        )

        return retriever

    def cached(self, tenant_id: str) -> IndexedCorpus | None:
        return self._corpora.get(tenant_id)

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._corpora.clear()
            self._checked_at.clear()
            return

        self._corpora.pop(tenant_id, None)
        self._checked_at.pop(tenant_id, None)


_index = KnowledgeIndex()


def get_knowledge_index() -> KnowledgeIndex:
    return _index


async def warm_index(tenant_id: str | None = None) -> int:
    """预热：启动/观测入口调用，失败不致命。"""
    from app.db.session import async_session_factory

    target = tenant_id or settings.default_tenant_id

    async with async_session_factory() as session:
        retriever = await get_knowledge_index().get(session, target)

    return len(retriever) if retriever is not None else 0
