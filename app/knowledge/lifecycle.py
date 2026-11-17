# app/knowledge/lifecycle.py
"""知识生命周期：老化识别与增量状态。

## 只管「挑出来」，不自动删

老化判定一定会误判：一份 2015 年的行业标准可能仍然有效（没有新版替代），
一份今年的操作手册可能因为项目下马而作废。所以这里只产出**候选清单 + 理由**，
下架是人的动作，并且走 ``documents.soft_delete``（可恢复、有审计）。

自动删的代价是不对称的：误删一次，检索静默少一片，没人会发现；
多留一个月，只是占几 MB。所以默认永不自动执行。

## 最该抓的不是「旧」，是「多版本并存」

这条是真踩过的坑：GB 27791 的 2020 版与 2011 版同时在库里，两版的章节
互相挤占候选位，同一批 23 条现场问题的覆盖从 5/23 掉到 3/23。旧版不会自己
消失——它内容完整、能被检索到、看起来一切正常。

所以 ``superseded`` 是唯一按「会自动伤害检索质量」定级的判据，
其余（年份偏老、解析失败、零切片）都只提示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import tagging
from app.models.document import Document, DocumentChunk, DocumentStatus, KnowledgeIndexState
from app.tenancy.context import tenant_scope

# 判定阈值。写死在这里而不是配置项：它们不是「开关」，是判定口径。
# 需要改的时候应该连带改文档与测试，而不是在控制台上滑一下。
MAX_AGE_YEARS = 6  # 标准一般 5 年复审一次，留一年缓冲
STALE_DAYS = 730  # 两年没动过的资料，值得看一眼

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


@dataclass
class Reason:
    code: str
    label: str
    detail: str
    severity: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "label": self.label,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class LifecycleItem:
    id: str
    title: str
    source_type: str
    standard: str = ""
    year: int | None = None
    chunks: int = 0
    indexed_at: str = ""
    reasons: list[Reason] = field(default_factory=list)

    @property
    def severity(self) -> str:
        order = {SEVERITY_HIGH: 3, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 1}
        return max(
            (reason.severity for reason in self.reasons),
            key=lambda value: order.get(value, 0),
            default=SEVERITY_LOW,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source_type": self.source_type,
            "standard": self.standard,
            "year": self.year,
            "chunks": self.chunks,
            "indexed_at": self.indexed_at,
            "severity": self.severity,
            # 建议动作只给「下架」与「看一眼」两种。给一堆动作等于不给。
            "suggest": "retire" if any(r.code == "superseded" for r in self.reasons) else "review",
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class _Row:
    """判定用的一行。与 documents.DocumentRow 分开：那边要给前端展示，
    这里只关心判定所需的列，别把两个用途耦在一起。"""

    id: str
    title: str
    filename: str | None
    source_type: str
    status: str
    chunks: int
    indexed_at: datetime | None
    created_at: datetime
    standard: str
    standard_base: str
    year: int | None
    snapshot: dict[str, Any]


def _tags_for(title: str, filename: str | None, snapshot: dict[str, Any]) -> tagging.DocTags:
    """优先用入库时的快照；没有（老文档）就现算。

    现算只看标题与文件名，不读正文：标题里就带着标准号的资料是绝大多数，
    而为了给老化页凑标签去把十几份 PDF 重新解析一遍，是把一个「看一眼」的
    页面做成了批量任务。
    """
    meta = (snapshot.get("metadata") or {}) if snapshot else {}
    standard = str(meta.get("standard") or "")

    if standard:
        return tagging.DocTags(
            tags=list(meta.get("tags") or []),
            standard=standard,
            standard_base=str(meta.get("standard_base") or ""),
            year=meta.get("year") if isinstance(meta.get("year"), int) else None,
            doctype=str(meta.get("doctype") or ""),
            topics=list(meta.get("topics") or []),
        )

    return tagging.analyze(title, filename=filename)


async def _rows(session: AsyncSession, tenant_id: str) -> list[_Row]:
    with tenant_scope(tenant_id):
        statement = (
            select(Document, func.count(DocumentChunk.id))
            .outerjoin(
                DocumentChunk,
                (DocumentChunk.document_id == Document.id) & (DocumentChunk.is_active.is_(True)),
            )
            .where(
                Document.tenant_id == tenant_id,
                # 已下架的不再出现在待办里，否则「处理完」永远清不掉。
                Document.status != DocumentStatus.DELETED,
            )
            .group_by(Document.id)
        )
        rows = (await session.execute(statement)).all()

    result: list[_Row] = []

    for row, count in rows:
        snapshot = (row.metadata_json or {}).get("pipeline") or {}
        tags = _tags_for(row.title, row.filename, snapshot)

        result.append(
            _Row(
                id=str(row.id),
                title=row.title,
                filename=row.filename,
                source_type=row.source_type,
                status=str(getattr(row.status, "value", row.status)),
                chunks=int(count or 0),
                indexed_at=row.indexed_at,
                created_at=row.created_at,
                standard=tags.standard,
                standard_base=tags.standard_base,
                year=tags.year,
                snapshot=snapshot,
            )
        )

    return result


def _superseded_ids(rows: list[_Row]) -> dict[str, str]:
    """找出「同一标准号的旧版本」，返回 {document_id: 新版本描述}。

    分成两步是因为两种情形要分开判：标准号带年份的，直接比年份；
    不带年份的同号多份（复印件、拆分上传），无法判新旧，只能用「切片多的是主本」
    这种弱判据——弱判据只提示、不判 retired。
    """
    groups: dict[str, list[_Row]] = {}

    for row in rows:
        if row.standard_base and row.year:
            groups.setdefault(row.standard_base, []).append(row)

    result: dict[str, str] = {}

    for group in groups.values():
        if len(group) < 2:
            continue

        newest = max(group, key=lambda item: item.year or 0)

        for row in group:
            if row.id == newest.id:
                continue

            # 同一年两版（一份是另一份的重切）不动：年份相同就没有「新版」，
            # 判谁是旧的只能靠猜。
            if (row.year or 0) >= (newest.year or 0):
                continue

            result[row.id] = f"{newest.standard}（{newest.year} 版，{newest.chunks} 片）"

    return result


async def inspect(
    session: AsyncSession,
    tenant_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """老化清单 + 增量状态。"""
    rows = await _rows(session, tenant_id)
    current = now or datetime.now(UTC)
    superseded = _superseded_ids(rows)
    items: list[LifecycleItem] = []

    for row in rows:
        reasons: list[Reason] = []

        if row.status == "failed":
            reasons.append(
                Reason(
                    code="failed",
                    label="解析或重建失败",
                    detail="这份文档当前不可检索，旧切片可能仍在但已不完整。",
                    severity=SEVERITY_HIGH,
                )
            )

        if row.chunks == 0 and row.status == "ready":
            reasons.append(
                Reason(
                    code="empty",
                    label="零切片",
                    detail="状态是 ready 但没有任何活跃切片，检索永远命中不到。",
                    severity=SEVERITY_HIGH,
                )
            )

        if row.id in superseded:
            reasons.append(
                Reason(
                    code="superseded",
                    label="已被新版本替代",
                    detail=f"库中已有更新的版本：{superseded[row.id]}。两版并存会互相挤占候选位。",
                    severity=SEVERITY_HIGH,
                )
            )

        if row.year and current.year - row.year >= MAX_AGE_YEARS:
            reasons.append(
                Reason(
                    code="outdated",
                    label=f"版本已 {current.year - row.year} 年",
                    detail=(
                        f"标准号显示为 {row.standard}。标准通常 5 年复审，"
                        "长期未复审的版本条文可能已修订。"
                    ),
                    severity=SEVERITY_MEDIUM,
                )
            )

        indexed = row.indexed_at or row.created_at

        if indexed and (current - indexed.astimezone(UTC)) > timedelta(days=STALE_DAYS):
            reasons.append(
                Reason(
                    code="stale",
                    label="长期未更新",
                    detail=f"最近一次入库是 {indexed.date().isoformat()}，之后没有重建。",
                    severity=SEVERITY_LOW,
                )
            )

        if reasons:
            items.append(
                LifecycleItem(
                    id=row.id,
                    title=row.title,
                    source_type=row.source_type,
                    standard=row.standard,
                    year=row.year,
                    chunks=row.chunks,
                    indexed_at=indexed.astimezone(UTC).isoformat() if indexed else "",
                    reasons=reasons,
                )
            )

    items.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item.severity], -item.chunks))

    cutoff = current - timedelta(days=30)

    return {
        "items": [item.to_dict() for item in items],
        "summary": {
            "documents": len(rows),
            "flagged": len(items),
            "superseded": sum(1 for item in items if item.id in superseded),
            "outdated": sum(
                1 for item in items if any(reason.code == "outdated" for reason in item.reasons)
            ),
            "broken": sum(
                1
                for item in items
                if any(reason.code in {"failed", "empty"} for reason in item.reasons)
            ),
        },
        "incremental": await _incremental(session, tenant_id, rows, cutoff),
        "policy": {
            "max_age_years": MAX_AGE_YEARS,
            "stale_days": STALE_DAYS,
            "auto_retire": False,
            "note": "只产出候选清单，下架由人确认；删除走软删，可恢复。",
        },
    }


async def _incremental(
    session: AsyncSession,
    tenant_id: str,
    rows: list[_Row],
    cutoff: datetime,
) -> dict[str, Any]:
    """增量更新的现状。

    「上次同步」取索引版本号的更新时间，而不是某条审计记录：索引版本每次
    入库都会自增，它才是「库里内容变了」的权威时间点。
    """
    with tenant_scope(tenant_id):
        state = await session.get(KnowledgeIndexState, tenant_id)
        updated_at = state.updated_at if state is not None else None

    recent = [
        row
        for row in rows
        if (row.indexed_at or row.created_at).astimezone(UTC) >= cutoff
    ]

    return {
        "index_version": f"v{int(state.version) if state is not None else 0}",
        "last_change_at": updated_at.astimezone(UTC).isoformat() if updated_at else "",
        "recent_documents": len(recent),
        "recent_chunks": sum(row.chunks for row in recent),
        "window_days": 30,
        "note": (
            "增量更新按内容哈希判定：内容与切片数都没变的文档不重建；"
            "目录里消失的文档自动下架。上传文档不受目录扫描影响。"
        ),
    }


async def retire(
    session: AsyncSession,
    tenant_id: str,
    document_ids: list[str],
) -> dict[str, Any]:
    """批量下架。逐份走 ``documents.soft_delete``（含版本自增与审计）。

    不做「一键下架所有候选」：候选里混着 low 级的「长期未更新」，
    那些多半不该删。让调用方显式给 id 列表，是把判断留在人那一侧。
    """
    from app.knowledge import documents as document_service

    retired: list[str] = []
    missing: list[str] = []

    for document_id in document_ids:
        ok = await document_service.soft_delete(session, tenant_id, document_id)

        (retired if ok else missing).append(document_id)

    return {"retired": retired, "missing": missing}
