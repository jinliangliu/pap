# app/knowledge/evaluation.py
"""检索效果评估：判分、汇总、在线跑一遍。

## 指标口径

三个数容易混，这里定死：

- **Recall@k** —— 该命中的资料命中了几成。一条用例可以标注多份相关文档
  （同一问题在三份资料里都有答案），召回 2/3 与召回 1/1 必须区分开，
  所以按「命中的标注数 / 标注总数」算，而不是布尔。
- **Precision@k** —— 返回的 k 条里有几成是相关的。只算有标注的用例；
  返回 4 条命中 1 条就是 0.25，哪怕排在第 1 位。它惩罚「多召回凑数」。
- **Hit@k** —— 至少命中一条。最宽松，适合看趋势；它会把「召回 1/3」算成满分，
  所以不能单独用它评估效果。

另有两条专治自欺：

- **Zero-hit rate** —— 可回答却零召回的比例。这是「召回上限」的直接证据，
  也是判断该不该上向量检索的唯一量化依据。
- **False-hit rate** —— 不可回答却返回了内容。只统计正例的评测集，
  「什么都答」看起来分很高。

## 阈值：召回阶段关掉，终选与线上一致

在线检索有 `min_score` 与 `min_relative_score` 两道阈值，它们属于**产品策略**
（宁可说不知道，也不给半相关的片段）。评测在**召回阶段**把它们关掉：
把阈值混进「召回得没得到」，会让「被阈值裁掉」记成「召回不到」，
优化方向就变成调阈值而不是补语料。

但评测也不能只测 BM25。线上返回的是「召回池 → 语义重筛 → top_k」，
只测第一步会报出用户根本遇不到的错：`调压器 膜片 破损 表现` 在 BM25 前 5
里没有 6.9 失效状态，重筛却能从 12 个候选里把它排到第一——评测说 miss，
用户拿到答案。所以逐条走 `service._retrieve`：召回池照旧放宽，终选照旧由
重筛与 `top_k` 决定，与对话、验证接口同一条路。

ACL 过滤始终保留：权限必须生效，否则评出来的分数包含用户根本看不到的内容。
"""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_gov import (
    CASE_SOURCE_IMPORT,
    VALID_CASE_SOURCES,
    KnowledgeEvalCase,
)
from app.tenancy.context import tenant_scope

CASE_LIMIT = 500

# 一条用例的判分结果。
@dataclass(frozen=True)
class CaseResult:
    case_id: str
    query: str
    expect_found: bool
    found: bool
    hit: bool
    # 命中的标注数 / 标注总数。
    recall: float = 0.0
    # 返回切片中相关的比例。
    precision: float = 0.0
    reciprocal_rank: float = 0.0
    # 召回的切片来源（document_id），按排序。
    sources: list[str] = field(default_factory=list)
    # 用例**标注**的相关文档。判分与「是否参与计分」都看它，
    # 不能拿 sources 顶：sources 是「召回了什么」，两个概念混起来会
    # 让没标注的用例只要召回了就被算进正例分母。
    relevant: list[str] = field(default_factory=list)
    # 命中了哪几份标注文档（logical_id）。复盘时一眼能看出「命中了另一份」。
    matched: list[str] = field(default_factory=list)
    error: str = ""
    # 每条用例耗时，汇总里出 p50/p95。
    elapsed_ms: int = 0
    # 语义重筛/回落的现场（http 模式才有）。评测脚本要用它区分
    # 「没召回」与「召回了被重筛判不相关」——两者的处置完全相反。
    rerank: dict[str, Any] = field(default_factory=dict)

    @property
    def is_false_hit(self) -> bool:
        return not self.expect_found and self.found

    @property
    def scored(self) -> bool:
        """是否参与计分：负例参与，正例必须标了期望文档。"""
        return (not self.expect_found) or bool(self.relevant)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "query": self.query,
            "expect_found": self.expect_found,
            "found": self.found,
            "hit": self.hit,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "sources": self.sources,
            "relevant": self.relevant,
            "matched": self.matched,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "rerank": self.rerank,
        }


def _relevant_of(case: dict[str, Any]) -> set[str]:
    """标注的相关文档。兼容两种写法：``expect_document``（库内用例）与
    ``relevant``（golden fixture 里的旧字段名）。"""
    raw = case.get("expect_document") or case.get("relevant") or []
    return {str(item) for item in raw if item}


def _section_ok(case: dict[str, Any], chunk: dict[str, Any]) -> bool:
    """填了 expect_section 就必须在大致对的那一段命中。

    「命中同一份标准的任意一片」对 448 片的国标来说太宽：问「5.1.6 过滤精度」
    却回第 3 章的术语定义，人要判它错，而按文档判分是满分。这个加强判据就是
    为这种情况准备的。留空则不加强。
    """
    keyword = str(case.get("expect_section") or "").strip()

    if not keyword:
        return True

    return keyword.lower() in str(chunk.get("section") or "").lower()


def score_case(
    case: dict[str, Any],
    retrieved: list[dict[str, Any]] | list[str],
    *,
    extra: dict[str, Any] | None = None,
) -> CaseResult:
    """按标注判一条用例。

    ``retrieved`` 是 top-k 的切片（按排序），每片至少要有 ``document_id``，
    有 ``section`` 时会用来做加强判据。也接受 ``list[str]``（来源标识列表）：
    ``scripts/eval_knowledge.py`` 的 local 模式手里只有路径，没有切片对象。
    """
    if retrieved and isinstance(retrieved[0], str):
        # 老调用方传的是标识列表。判分逻辑不变，只是少了 section 加强判据。
        chunks: list[dict[str, Any]] = [
            {"document_id": str(item), "section": ""} for item in retrieved
        ]
    else:
        chunks = retrieved  # type: ignore[assignment]

    relevant = _relevant_of(case)
    expect_found = bool(case.get("expect_found", True))
    sources = [str(chunk.get("document_id") or "") for chunk in chunks]
    matched: list[str] = []
    reciprocal_rank = 0.0
    relevant_hits = 0
    seen_hit_doc: set[str] = set()

    for rank, chunk in enumerate(chunks, start=1):
        document_id = str(chunk.get("document_id") or "")
        is_relevant = document_id in relevant and _section_ok(case, chunk)

        if is_relevant:
            relevant_hits += 1

            if document_id not in seen_hit_doc:
                seen_hit_doc.add(document_id)
                matched.append(document_id)

            if reciprocal_rank == 0.0:
                reciprocal_rank = 1.0 / rank

    # 负例（库里本就没有）不给 recall/precision：分母是 0，任何取值都是编的。
    recall = len(matched) / len(relevant) if (expect_found and relevant) else 0.0
    precision = relevant_hits / len(chunks) if (expect_found and chunks) else 0.0

    payload: dict[str, Any] = {
        "case_id": str(case.get("id") or case.get("case_id") or ""),
        "query": str(case.get("query") or ""),
        "expect_found": expect_found,
        "found": bool(retrieved),
        "hit": reciprocal_rank > 0.0,
        "recall": recall,
        "precision": precision,
        "reciprocal_rank": reciprocal_rank,
        "sources": sources,
        "relevant": sorted(relevant),
        "matched": matched,
    }
    payload.update(extra or {})

    return CaseResult(**payload)


def summarize(results: list[CaseResult], *, k: int) -> dict[str, Any]:
    """把逐条结果压成一组指标。

    分母全部用**参与计分的用例数**，不是总数：把没标注期望文档的用例算进
    分母，只会让指标无故变低，然后有人去「修」一个并不存在的问题。
    """
    positive = [item for item in results if item.expect_found and item.scored]
    negative = [item for item in results if not item.expect_found]
    unscored = [item for item in results if item.expect_found and not item.scored]
    errors = [item for item in results if item.error]
    elapsed = [item.elapsed_ms for item in results if item.elapsed_ms]

    def rate(count: int, total: int) -> float:
        return round(count / total, 4) if total else 0.0

    reranked = [item.rerank for item in results if item.rerank]
    fallbacks: dict[str, int] = {}

    for payload in reranked:
        reason = str(payload.get("fallback") or "")

        if reason:
            fallbacks[reason] = fallbacks.get(reason, 0) + 1

    return {
        "k": k,
        "cases": len(results),
        "scored": len(positive) + len(negative),
        "positive": len(positive),
        "negative": len(negative),
        # 未标注期望文档的正例：存下来了但还没法判分。不静默忽略——
        # 「我加了 50 条用例」和「其中 40 条没标答案」是两件事。
        "unscored": len(unscored),
        "errors": len(errors),
        "recall": round(
            statistics.fmean(item.recall for item in positive) if positive else 0.0, 4
        ),
        "precision": round(
            statistics.fmean(item.precision for item in positive) if positive else 0.0, 4
        ),
        "hit_at_k": rate(sum(1 for item in positive if item.hit), len(positive)),
        "mrr": round(
            statistics.fmean(item.reciprocal_rank for item in positive) if positive else 0.0, 4
        ),
        "zero_hit_rate": rate(sum(1 for item in positive if not item.found), len(positive)),
        # 同义别名，留给既有脚本与报告（口径完全一样，只是名字更明确）。
        "answerable_zero_hit_rate": rate(
            sum(1 for item in positive if not item.found), len(positive)
        ),
        "false_hit_rate": rate(sum(1 for item in negative if item.found), len(negative)),
        "elapsed_p50_ms": int(statistics.median(elapsed)) if elapsed else 0,
        "elapsed_p95_ms": (
            int(sorted(elapsed)[min(len(elapsed) - 1, int(len(elapsed) * 0.95))]) if elapsed else 0
        ),
        "rerank_applied": sum(1 for payload in reranked if payload.get("applied")),
        "rerank_fallbacks": fallbacks,
        "rerank_dropped": sum(int(payload.get("dropped", 0)) for payload in reranked),
        # 重筛耗时来自接口返回的 rerank 明细（那里才有真正花在模型上的时间）。
        "rerank_elapsed_p50_ms": _percentile(
            [int(payload.get("elapsed_ms") or 0) for payload in reranked], 50
        ),
        "rerank_elapsed_p95_ms": _percentile(
            [int(payload.get("elapsed_ms") or 0) for payload in reranked], 95
        ),
    }


async def run_online(
    session: AsyncSession,
    tenant_id: str,
    cases: list[dict[str, Any]],
    *,
    k: int = 5,
    scopes: Collection[str],
    roles: tuple[str, ...] = (),
    user_id: str = "",
) -> tuple[dict[str, Any], list[CaseResult]]:
    """用**线上那条检索链路**跑一遍。返回 (汇总, 逐条结果)。

    走 ``service._retrieve`` 而不是直接调 ``retriever.search``：线上返回的是
    「BM25 召回池 → 语义重筛 → 留 top_k」。只测 BM25 会报出一个用户根本
    遇不到的错：`调压器 膜片 破损 表现` 在 BM25 前 5 里没有 6.9 失效状态，
    但重筛从 12 个候选里把它排到了第一——评测说 miss，用户却拿到了答案。

    阈值仍在召回阶段关掉（``_retrieve`` 里重筛开启时 min_score=0），
    最终条数由重筛与 ``top_k`` 决定；ACL 过滤在打分之前，评测不能看到
    用户看不到的内容。

    逐条结果里带上 ``rerank`` 明细：知道「召回了又被重筛全剔」和
    「一条都没召回」是两件事，处置方向完全相反。
    """
    # 延迟导入：service 会牵进 agents，而 agents 又引用 knowledge，
    # 在模块顶层导入会绕成环。
    from app.knowledge.index import get_knowledge_index
    from app.knowledge.service import _retrieve

    retriever = await get_knowledge_index().get(session, tenant_id)

    if retriever is None:
        raise RuntimeError("知识索引为空，无法评测。请先入库资料。")

    results: list[CaseResult] = []

    for case in cases:
        started = time.monotonic()

        try:
            retrieval = await _retrieve(
                query=str(case.get("query") or ""),
                scopes=scopes,
                tenant_id=tenant_id,
                roles=roles,
                user_id=user_id,
                top_k=k,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            results.append(
                score_case(
                    case,
                    retrieval.documents,
                    extra={"elapsed_ms": elapsed, "rerank": retrieval.rerank.to_dict()},
                )
            )
        except Exception as exc:  # 单条失败不该让整次评测没有结果
            elapsed = int((time.monotonic() - started) * 1000)
            results.append(
                score_case(
                    case,
                    [],
                    extra={"elapsed_ms": elapsed, "error": str(exc)[:300]},
                )
            )

    return summarize(results, k=k), results


# ---------------------------------------------------------------- 标注集维护


class CaseRejected(ValueError):
    """调用方能自己修正的输入问题（映射 400）。"""


def case_to_dict(row: KnowledgeEvalCase) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "query": row.query,
        "expect_found": bool(row.expect_found),
        "expect_document": list(row.expect_document or []),
        "expect_section": row.expect_section,
        "category": row.category,
        "note": row.note,
        "source": row.source,
        "enabled": bool(row.enabled),
        # 没标期望文档就用不了：界面据此显示「待标注」而不是「0 分」。
        "scored": bool(row.expect_document) or not row.expect_found,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


async def list_cases(
    session: AsyncSession,
    tenant_id: str,
    *,
    enabled: bool | None = None,
    limit: int = CASE_LIMIT,
) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id):
        statement = select(KnowledgeEvalCase).where(KnowledgeEvalCase.tenant_id == tenant_id)

        if enabled is not None:
            statement = statement.where(KnowledgeEvalCase.enabled.is_(enabled))

        statement = (
            statement.order_by(KnowledgeEvalCase.created_at.desc())
            .limit(max(1, min(limit, 1000)))
        )
        rows = list((await session.scalars(statement)).all())

    return [case_to_dict(row) for row in rows]


def _validate_case(
    *,
    query: str,
    expect_found: bool,
    expect_document: list[str],
    source: str,
) -> None:
    if not (query or "").strip():
        raise CaseRejected("用例必须有问题。")

    if source not in VALID_CASE_SOURCES:
        raise CaseRejected(f"未知的 source：{source}")

    # 可回答用例可以不标期望文档（先记下来待补），但一旦标了就不能是空串：
    # 空串永远匹配不到任何切片，指标会稳定地算成「召回 0」，而人以为是模型退化了。
    if any(not str(item).strip() for item in expect_document):
        raise CaseRejected("expect_document 里不能有空值。")

    if not expect_found and expect_document:
        raise CaseRejected("「库里没有答案」的用例不该标期望文档。")


async def upsert_case(
    session: AsyncSession,
    tenant_id: str,
    *,
    case_id: str | None,
    query: str,
    expect_found: bool,
    expect_document: list[str],
    expect_section: str | None,
    category: str | None,
    note: str | None,
    enabled: bool,
    source: str,
    actor: str | None,
) -> dict[str, Any]:
    """新建或修改一条用例。``case_id`` 为空即新建。"""
    _validate_case(
        query=query,
        expect_found=expect_found,
        expect_document=expect_document,
        source=source,
    )

    target = _as_uuid(case_id) if case_id else None

    with tenant_scope(tenant_id):
        row = await session.get(KnowledgeEvalCase, target) if target else None

        if target and (row is None or row.tenant_id != tenant_id):
            raise CaseRejected("用例不存在或不属于当前租户。")

        if row is None:
            row = KnowledgeEvalCase(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                created_at=datetime.now(UTC),
            )
            session.add(row)

        row.query = query.strip()
        row.expect_found = expect_found
        row.expect_document = [str(item) for item in expect_document]
        row.expect_section = (expect_section or None) and expect_section[:256]
        row.category = (category or None) and category[:64]
        row.note = (note or None) and note[:2000]
        row.enabled = enabled
        # 来源不改写：一条从反馈升格的用例被人补了标注，它仍然是「从事故来的」，
        # 这个出身决定了复盘时该不该优先看它。
        row.source = row.source or source
        row.created_by = row.created_by or actor
        # 时间字段是库端 onupdate，异步会话里读回来会 MissingGreenlet，显式赋。
        row.updated_at = datetime.now(UTC)
        await session.flush()
        payload = case_to_dict(row)

    return payload


async def delete_case(session: AsyncSession, tenant_id: str, case_id: str) -> bool:
    target = _as_uuid(case_id)

    if target is None:
        return False

    with tenant_scope(tenant_id):
        row = await session.get(KnowledgeEvalCase, target)

        if row is None or row.tenant_id != tenant_id:
            return False

        await session.delete(row)
        await session.flush()

    return True


async def import_cases(
    session: AsyncSession,
    tenant_id: str,
    payload: list[dict[str, Any]],
    *,
    actor: str | None,
) -> dict[str, Any]:
    """批量导入用例（从 fixture 或手工整理的 JSON）。

    逐条走 ``upsert_case`` 同一份校验：导入路径最容易绕过规则，
    一个空串 expect_document 从这里进来就会长期污染指标。
    """
    created = 0
    rejected: list[dict[str, str]] = []

    for index, item in enumerate(payload):
        try:
            await upsert_case(
                session,
                tenant_id,
                case_id=None,
                query=str(item.get("query") or item.get("question") or ""),
                expect_found=bool(item.get("expect_found", True)),
                expect_document=[
                    str(value)
                    for value in (
                        item.get("expect_document")
                        or item.get("relevant")
                        or item.get("document_ids")
                        or []
                    )
                ],
                expect_section=item.get("expect_section") or item.get("section"),
                category=item.get("category"),
                note=item.get("note"),
                enabled=bool(item.get("enabled", True)),
                source=CASE_SOURCE_IMPORT,
                actor=actor,
            )
            created += 1
        except CaseRejected as exc:
            rejected.append({"index": str(index), "reason": str(exc)})

    return {"created": created, "rejected": rejected}


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _percentile(values: list[int], percent: int) -> int:
    """整数分位。样本为空返回 0——「还没跑」不是「很快」。"""
    if not values:
        return 0

    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * percent / 100))
    return int(ordered[index])


def _percentile(values: list[int], percent: int) -> int:
    """整数分位。样本为空返回 0——「还没跑」不是「很快」。"""
    if not values:
        return 0

    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * percent / 100))
    return int(ordered[index])
