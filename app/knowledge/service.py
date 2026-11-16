# app/knowledge/service.py
"""知识问答：检索 + 可追溯引用。

回答必须区分“资料明确说明”与“资料不足”，
禁止用模型常识填补成资料结论。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.agents.prompts import (
    KNOWLEDGE_EMPTY_ANSWER,
    KNOWLEDGE_EXCERPT_TEMPLATE,
    KNOWLEDGE_FOOTER,
    KNOWLEDGE_HEADER,
    KNOWLEDGE_REJECTED_ANSWER,
)
from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge.index import get_knowledge_index
from app.knowledge.rerank import RerankMeta, rerank
from app.knowledge.retriever import (
    Citation,
    KnowledgeRetriever,
    allowed_scopes_for,
)

MAX_EXCERPT_CHARS = 400


async def _retriever_for(tenant_id: str | None) -> KnowledgeRetriever | None:
    """
    取当前租户的检索器。

    索引内容来自数据库，按指纹热加载：重新入库后无需重启进程，
    多实例也会在下一次检索时自动收敛到同一版本。
    """
    target = tenant_id or settings.default_tenant_id

    async with async_session_factory() as session:
        return await get_knowledge_index().get(session, target)


@dataclass
class KnowledgeAnswer:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    # 语义重筛诊断，供检索验证页显示「生效 / 回落 / 剔除了几条」。
    rerank: dict[str, Any] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.documents)


@dataclass
class Retrieval:
    documents: list[dict[str, Any]] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    rerank: RerankMeta = field(default_factory=RerankMeta)


async def _retrieve(
    *,
    query: str,
    scopes: Iterable[str],
    tenant_id: str | None,
    roles: Iterable[str],
    user_id: str,
    top_k: int,
) -> Retrieval:
    """问答链路、验证接口、工具层共用的检索入口。

    召回池比重筛后的返回条数宽：BM25 低分但语义相关的片段必须先有资格
    被模型看到。两处阈值在召回阶段都关掉，最终条数由 ``top_k`` 决定。
    权限过滤仍在 ``retriever.search`` 内部、打分之前完成——扩大召回池
    不等于放宽权限。
    """
    retriever = await _retriever_for(tenant_id)

    if retriever is None:
        return Retrieval()

    enabled = settings.knowledge_rerank_enabled
    recall_k = max(top_k, settings.knowledge_rerank_candidates) if enabled else top_k
    documents, citations = retriever.search(
        query=query,
        allowed_scopes=allowed_scopes_for(scopes),
        roles=roles,
        user_id=user_id,
        top_k=recall_k,
        min_score=0 if enabled else None,
        min_relative_score=0 if enabled else None,
    )

    result = await rerank(query=query, documents=documents, top_k=top_k)

    return Retrieval(
        documents=result.documents,
        citations=[citations[index] for index in result.indices if index < len(citations)],
        rerank=result.meta,
    )


async def answer_from_knowledge(
    *,
    query: str,
    scopes: Iterable[str],
    tenant_id: str | None = None,
    roles: Iterable[str] = (),
    user_id: str = "",
    top_k: int | None = None,
) -> KnowledgeAnswer:
    retrieval = await _retrieve(
        query=query,
        scopes=scopes,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        # /knowledge/search 的 limit 以前被接口吞掉了，评测无法测 Recall@k。
        # 这里透传，默认仍是配置里的 knowledge_top_k。
        top_k=settings.knowledge_top_k if top_k is None else top_k,
    )
    documents = retrieval.documents

    if not documents:
        if retrieval.rerank.applied and retrieval.rerank.dropped:
            # 重筛把候选全剔了：说清「查过、但都不足以回答」，
            # 不要和「一条都没召回」混成同一句「资料不足」。
            answer = KNOWLEDGE_REJECTED_ANSWER.format(count=retrieval.rerank.dropped)
        else:
            answer = KNOWLEDGE_EMPTY_ANSWER

        return KnowledgeAnswer(answer=answer, rerank=retrieval.rerank.to_dict())

    excerpts = "\n\n".join(
        KNOWLEDGE_EXCERPT_TEMPLATE.format(
            index=index + 1,
            title=document.get("title", ""),
            section=f"·{document.get('section', '')}",
            updated_at=document.get("updated_at", "")[:10],
            text=str(document.get("text", ""))[:MAX_EXCERPT_CHARS],
        )
        for index, document in enumerate(documents)
    )

    answer = "\n".join(
        [
            KNOWLEDGE_HEADER.format(count=len(documents)),
            excerpts,
            KNOWLEDGE_FOOTER,
        ]
    )

    return KnowledgeAnswer(
        answer=answer,
        citations=[item.to_dict() for item in retrieval.citations],
        documents=documents,
        rerank=retrieval.rerank.to_dict(),
    )


async def search_as_tool(
    *,
    query: str,
    scopes: Iterable[str],
    limit: int = 4,
    tenant_id: str | None = None,
    roles: Iterable[str] = (),
    user_id: str = "",
) -> tuple[list[dict[str, Any]], list[Citation]]:
    """供工具层复用的纯检索入口。"""
    retrieval = await _retrieve(
        query=query,
        scopes=scopes,
        tenant_id=tenant_id,
        roles=roles,
        user_id=user_id,
        top_k=limit,
    )

    return retrieval.documents, retrieval.citations
