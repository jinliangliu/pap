# app/knowledge/pipeline.py
"""知识加工与治理的流水线：阶段定义 + 实际产出指标。

## 这一层要解决什么

入库是「一份文件进来、一堆切片出去」，中间五步全在一个调用栈里，跑完就没了。
于是治理页只能显示「13 份文档 / 1332 个切片」这两个结果数，没人知道：

- 上传的是 56.8 万字，入库的为什么只有 14 万字（清洗掉了什么）；
- 一份标准被切成 448 片，是切碎了还是有 448 个小节；
- 标签覆盖率是 0 还是没采集。

所以入库时把每一步的产物量写进 ``documents.metadata_json["pipeline"]``，
读取时按文档聚合。**没有快照的老文档单独计数，不按 0 算**——「没有数据」
和「结果是 0」在治理页上是两种完全不同的结论，混在一起就没法判断该不该重建。

## 三个状态，不是一个进度条

``active`` / ``partial`` / ``not_enabled``。

向量化与知识图谱在这套系统里**没有实现**，它们不是「完成了 0%」，而是
「这条路没走」。画一条停在 60% 的进度条会让人以为再等等就好，而真实情况是
BM25 + 语义重筛已经能把召回做到可接受，上向量检索要等「零字面交集」的
证据出现（见 docs/KNOWLEDGE.md）。所以这两个阶段返回 ``not_enabled`` +
原因 + 触发条件，接口里也带 ``evidence`` 说明每个数字是怎么来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import tagging
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.tenancy.context import tenant_scope

STATUS_ACTIVE = "active"
STATUS_PARTIAL = "partial"
STATUS_NOT_ENABLED = "not_enabled"

GROUP_PROCESS = "process"
GROUP_STORE = "store"


@dataclass(frozen=True)
class StageSpec:
    """一个阶段的静态说明。前端只认这份定义，不在页面里另写一套文案。"""

    id: str
    name: str
    group: str
    # 实现这个阶段的代码位置。写出来不是为了好看：治理页说「已启用」，
    # 运维得能立刻找到它跑在哪，否则只能信我们一面之词。
    engine: str
    what: str


STAGE_SPECS: tuple[StageSpec, ...] = (
    StageSpec(
        id="parse",
        name="数据解析",
        group=GROUP_PROCESS,
        engine="app/knowledge/ingest.py: extract_text_from_bytes",
        what="按后缀分流：md/txt 直读，pdf/docx/html 抽文本；统一行尾，算内容哈希。",
    ),
    StageSpec(
        id="clean",
        name="数据清洗",
        group=GROUP_PROCESS,
        engine="app/knowledge/markdown_clean.py: clean_ocr_markdown",
        what="OCR 产物里的 HTML 表格转 pipe 表、合并单元格摊平、去重复页与跨页页眉。",
    ),
    StageSpec(
        id="structure",
        name="结构化提取",
        group=GROUP_PROCESS,
        engine="app/knowledge/ingest.py: split_document",
        what="按 Markdown 一至三级标题与中文编号标题切出小节，保留层级路径作为定位。",
    ),
    StageSpec(
        id="chunk",
        name="分段（Chunking）",
        group=GROUP_PROCESS,
        engine="app/knowledge/ingest.py: split_document / CHUNK_SIZE",
        what="小节内按 800 字滑窗切分、120 字重叠；单篇超上限直接拒收而不是偷偷截断。",
    ),
    StageSpec(
        id="metadata",
        name="元数据打标",
        group=GROUP_PROCESS,
        engine="app/knowledge/tagging.py: analyze",
        what="抽标准号、发布年份、文档类型、领域主题词，与分级/可见性一并写进切片元数据。",
    ),
    StageSpec(
        id="vector",
        name="向量化",
        group=GROUP_STORE,
        engine="—",
        what="切片转向量存 pgvector，用于零字面交集的语义召回。",
    ),
    StageSpec(
        id="index",
        name="索引",
        group=GROUP_STORE,
        engine="app/knowledge/index.py: KnowledgeIndex (BM25)",
        what="进程内 BM25 结构，按索引指纹热加载；入库后所有实例下一次检索即生效。",
    ),
    StageSpec(
        id="graph",
        name="知识图谱",
        group=GROUP_STORE,
        engine="—",
        what="实体关系抽取与图查询（设备—参数—规程之间的关联）。",
    ),
)

# 未实现的两个阶段：写清「为什么没有」和「什么条件下才该有」。
# 这不是免责声明，是给下一次决策留的判断依据——半年后再看，能知道当时
# 是漏做了还是权衡后没做。
NOT_ENABLED: dict[str, dict[str, str]] = {
    "vector": {
        "reason": (
            "当前检索是 BM25 词面召回 + 语义重筛，中文技术标准里问题与条文用词高度重合，"
            "尚未出现「零字面交集但语义相关」的确凿样本。"
        ),
        "trigger": (
            "当离线评测的 answerable_zero_hit_rate 持续高于 15%，"
            "且逐条核对确认是词面差异而非语料缺失时，才值得引入向量通道。"
        ),
    },
    "graph": {
        "reason": (
            "语料是规程与标准条文，不是「设备—部件—参数」的实体网络；"
            "没有实体标注，抽出来的关系无法验收。"
        ),
        "trigger": "接入设备台账并具备实体标注后，图谱才有可验证的查询场景。",
    },
}


def snapshot_of(document_meta: dict[str, Any] | None) -> dict[str, Any]:
    """取一份文档的流水线快照。没有就返回空 dict（**不是**填零的结构）。"""
    snapshot = (document_meta or {}).get("pipeline")

    return snapshot if isinstance(snapshot, dict) else {}


def build_snapshot(
    *,
    extractor: str,
    source_bytes: int | None,
    text_chars: int,
    clean_stats: dict[str, Any] | None,
    tags: tagging.DocTags,
    chunks: list[str],
    sections: list[str],
    chunker_version: int,
    chunk_size: int,
) -> dict[str, Any]:
    """入库时算一次快照，写进 ``documents.metadata_json["pipeline"]``。

    只存**可复算的原始量**（字数、片数、最大片长），不存推导出来的比率：
    均长这类值随切片规则变化，存下来就成了历史包袱，读的时候除一下即可。
    """
    lengths = [len(chunk) for chunk in chunks]
    stats = clean_stats or {}

    return {
        "parse": {
            "extractor": extractor,
            "bytes": source_bytes,
            "chars": text_chars,
        },
        # 没走到清洗（干净的 md）与「清洗后没省字」是两回事，前者不留这一节。
        "clean": (
            {
                "chars_before": int(stats.get("chars_before") or 0),
                "chars_after": int(stats.get("chars_after") or 0),
                "tables": int(stats.get("tables") or 0),
            }
            if stats
            else {}
        ),
        "structure": {
            "sections": len(set(sections)),
            # 实际产出的小节数（切片可能比小节多：一个小节超长会被再切）。
            "section_refs": len(sections),
        },
        "chunk": {
            "chunks": len(chunks),
            "chars": sum(lengths),
            "max_chars": max(lengths) if lengths else 0,
            "chunker_version": chunker_version,
            "chunk_size": chunk_size,
        },
        "metadata": tags.to_dict(),
    }


def extractor_of(filename: str | None, source_uri: str | None = None) -> str:
    """解析器名字。与 ``extract_text_from_bytes`` 的分流保持一致。"""
    from pathlib import Path

    suffix = Path(filename or source_uri or "").suffix.lower().lstrip(".")
    return suffix or "unknown"


# ---------------------------------------------------------------- 聚合


@dataclass(frozen=True)
class _DocFacts:
    """聚合用的一份文档。只为算指标，不对外暴露。"""

    id: str
    title: str
    filename: str | None
    source_type: str
    status: str
    chunks: int
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class ChunkFacts:
    total: int
    avg_chars: int
    max_chars: int
    min_chars: int
    oversized: int


async def _documents(session: AsyncSession, tenant_id: str) -> list[_DocFacts]:
    with tenant_scope(tenant_id):
        statement = (
            select(Document, func.count(DocumentChunk.id))
            .outerjoin(
                DocumentChunk,
                (DocumentChunk.document_id == Document.id) & (DocumentChunk.is_active.is_(True)),
            )
            .where(
                Document.tenant_id == tenant_id,
                Document.status != DocumentStatus.DELETED,
            )
            .group_by(Document.id)
            .order_by(Document.created_at.desc())
        )
        rows = (await session.execute(statement)).all()

    return [
        _DocFacts(
            id=str(row.id),
            title=row.title,
            filename=row.filename,
            source_type=row.source_type,
            status=str(getattr(row.status, "value", row.status)),
            chunks=int(count or 0),
            snapshot=snapshot_of(row.metadata_json),
        )
        for row, count in rows
    ]


async def _chunk_facts(session: AsyncSession, tenant_id: str, chunk_size: int) -> ChunkFacts:
    """切片级指标一条 SQL 出完。1332 片也只是一次聚合，不要按文档循环查。"""
    length = func.length(DocumentChunk.content)

    with tenant_scope(tenant_id):
        row = (
            await session.execute(
                select(
                    func.count(DocumentChunk.id),
                    func.coalesce(func.avg(length), 0),
                    func.coalesce(func.max(length), 0),
                    func.coalesce(func.min(length), 0),
                ).where(
                    DocumentChunk.tenant_id == tenant_id,
                    DocumentChunk.is_active.is_(True),
                )
            )
        ).one()

        # 超长片单独一条：只有「单段本身超长、硬切也切不开」那条分支会产出
        # 大于 chunk_size 的片子。数量多说明语料没有自然换行（典型是 OCR 产物）。
        oversized = await session.scalar(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.is_active.is_(True),
                length > chunk_size,
            )
        )

    return ChunkFacts(
        total=int(row[0] or 0),
        avg_chars=int(row[1] or 0),
        max_chars=int(row[2] or 0),
        min_chars=int(row[3] or 0),
        oversized=int(oversized or 0),
    )


def _coverage(docs: list[_DocFacts], key: str) -> tuple[int, int]:
    """有这一节快照的文档数 / 有切片的文档数。"""
    ready = [doc for doc in docs if doc.chunks > 0]
    return sum(1 for doc in ready if doc.snapshot.get(key)), len(ready)


def _stage(
    spec: StageSpec,
    *,
    status: str,
    metrics: list[dict[str, Any]],
    evidence: str,
    note: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": spec.id,
        "name": spec.name,
        "group": spec.group,
        "engine": spec.engine,
        "what": spec.what,
        "status": status,
        "metrics": metrics,
        # 每个数字的依据。治理页最容易变成「看起来很专业但没人知道真假」，
        # 这一栏是把它拉回可核对的那一半。
        "evidence": evidence,
    }

    if note:
        payload["note"] = note

    if status == STATUS_NOT_ENABLED:
        payload.update(NOT_ENABLED.get(spec.id, {}))

    return payload


async def overview(session: AsyncSession, tenant_id: str) -> dict[str, Any]:
    """治理页的全部数据：八个阶段 + 总量。"""
    from app.core.config import settings
    from app.knowledge import store
    from app.knowledge.ingest import CHUNK_SIZE, CHUNKER_VERSION

    docs = await _documents(session, tenant_id)
    chunks = await _chunk_facts(session, tenant_id, CHUNK_SIZE)
    index_state = await store.stats(session, tenant_id)

    active_docs = [doc for doc in docs if doc.chunks > 0]
    parsed, parse_den = _coverage(docs, "parse")
    cleaned, clean_den = _coverage(docs, "clean")
    structured, struct_den = _coverage(docs, "structure")
    tagged, tag_den = _coverage(docs, "metadata")

    # 清洗这一步天然不是每份都走：干净的 md 不进清洗分支。所以「覆盖率 29%」
    # 不能读成「71% 没洗」。把两种情形在 note 里说清楚，别让人对着数字猜。
    clean_ran = sum(1 for doc in active_docs if doc.snapshot.get("clean"))
    total_chars = sum(
        int((doc.snapshot.get("parse") or {}).get("chars") or 0) for doc in active_docs
    )
    saved_chars = sum(
        max(
            0,
            int((doc.snapshot.get("clean") or {}).get("chars_before") or 0)
            - int((doc.snapshot.get("clean") or {}).get("chars_after") or 0),
        )
        for doc in active_docs
    )
    tables = sum(int((doc.snapshot.get("clean") or {}).get("tables") or 0) for doc in active_docs)
    sections = sum(int((doc.snapshot.get("structure") or {}).get("sections") or 0) for doc in docs)
    tag_empty = sum(
        1
        for doc in active_docs
        if not ((doc.snapshot.get("metadata") or {}).get("tags") or [])
    )
    standards = sorted(
        {
            str((doc.snapshot.get("metadata") or {}).get("standard"))
            for doc in docs
            if (doc.snapshot.get("metadata") or {}).get("standard")
        }
    )

    doc_types: dict[str, int] = {}

    for doc in docs:
        name = str((doc.snapshot.get("metadata") or {}).get("doctype") or "未识别")
        doc_types[name] = doc_types.get(name, 0) + 1

    def fmt_cover(have: int, total: int) -> str:
        return f"{have}/{total}" if total else "—"

    stages = [
        _stage(
            STAGE_SPECS[0],
            status=STATUS_ACTIVE if parsed else STATUS_PARTIAL,
            metrics=[
                {"label": "已解析", "value": len(active_docs), "unit": "篇"},
                {"label": "正文字数", "value": total_chars, "unit": "字"},
                {"label": "快照覆盖", "value": fmt_cover(parsed, parse_den)},
                {
                    "label": "上传占位",
                    "value": sum(1 for doc in active_docs if doc.source_type == "upload"),
                    "unit": "篇",
                },
            ],
            evidence="documents.metadata_json.pipeline.parse + 切片数 > 0 的文档",
            note=(
                f"{parse_den - parsed} 篇是在采集快照之前入库的，没有解析明细；"
                "重建一次即可补上。"
                if parse_den > parsed
                else ""
            ),
        ),
        _stage(
            STAGE_SPECS[1],
            status=STATUS_ACTIVE,
            metrics=[
                {"label": "走到清洗", "value": clean_ran, "unit": "篇"},
                {"label": "压掉字符", "value": saved_chars, "unit": "字"},
                {"label": "转 pipe 表", "value": tables, "unit": "张"},
                {"label": "快照覆盖", "value": fmt_cover(cleaned, clean_den)},
            ],
            evidence="documents.metadata_json.pipeline.clean（只有带 OCR 标签的产物才会记）",
            note=(
                "这一步按需触发：干净的 markdown 不进清洗分支，因此「走到清洗」"
                "的篇数少于文档总数是正常的，不是漏跑。"
            ),
        ),
        _stage(
            STAGE_SPECS[2],
            status=STATUS_ACTIVE if structured else STATUS_PARTIAL,
            metrics=[
                {"label": "识别小节", "value": sections, "unit": "个"},
                {
                    "label": "每篇均",
                    "value": round(sections / len(active_docs), 1) if active_docs else 0,
                    "unit": "节",
                },
                {"label": "快照覆盖", "value": fmt_cover(structured, struct_den)},
            ],
            evidence="split_document 产出的 Section.section 去重计数",
            note="只做章节结构解析，不做实体抽取——后者属于知识图谱阶段，当前未启用。",
        ),
        _stage(
            STAGE_SPECS[3],
            status=STATUS_ACTIVE,
            metrics=[
                {"label": "活跃切片", "value": chunks.total, "unit": "片"},
                {"label": "平均长度", "value": chunks.avg_chars, "unit": "字"},
                {"label": "最长 / 最短", "value": f"{chunks.max_chars} / {chunks.min_chars}"},
                {"label": "切片规则", "value": f"v{CHUNKER_VERSION} · {CHUNK_SIZE} 字"},
            ],
            evidence="document_chunks 活跃行的一次聚合（count / avg / max / min(length)）",
        ),
        _stage(
            STAGE_SPECS[4],
            status=STATUS_ACTIVE if tagged else STATUS_PARTIAL,
            metrics=[
                {"label": "有标签", "value": fmt_cover(tagged, tag_den)},
                {"label": "识别标准", "value": len(standards), "unit": "个"},
                {"label": "无标签文档", "value": tag_empty, "unit": "篇"},
            ],
            evidence="tagging.analyze 写入切片的 tags / standard / year / doctype",
            note=(
                "；".join(f"{name} {count}" for name, count in sorted(doc_types.items()))
                if doc_types
                else ""
            ),
        ),
        _stage(
            STAGE_SPECS[5],
            status=STATUS_NOT_ENABLED,
            metrics=[
                {"label": "向量切片", "value": 0, "unit": "片"},
                {"label": "当前召回", "value": "BM25", "unit": "词面"},
            ],
            evidence=(
                "document_chunks.vector_id 全为空；检索链路只有 BM25 的写入与查询。"
                "数据库装了 pgvector 扩展，但没有向量写入路径，也没有查询路径。"
            ),
        ),
        _stage(
            STAGE_SPECS[6],
            status=STATUS_ACTIVE,
            metrics=[
                {"label": "索引版本", "value": index_state.version},
                {"label": "覆盖切片", "value": index_state.chunks, "unit": "片"},
                {"label": "结构", "value": "BM25 · 进程内"},
                {"label": "刷新", "value": f"{settings.knowledge_index_refresh_seconds}s 节流"},
            ],
            evidence="knowledge_index_state.version + KnowledgeIndex 指纹热加载",
            note="没有倒排落盘：索引在进程内存里，重启后首次检索时重建。",
        ),
        _stage(
            STAGE_SPECS[7],
            status=STATUS_NOT_ENABLED,
            metrics=[
                {"label": "实体", "value": 0, "unit": "个"},
                {"label": "关系", "value": 0, "unit": "条"},
            ],
            evidence="无实体/关系表，检索链路里没有图查询",
        ),
    ]

    return {
        "stages": stages,
        "totals": {
            "documents": len(active_docs),
            "chunks": chunks.total,
            "chars": total_chars,
            "sections": sections,
            "standards": standards,
            "chunk_size": CHUNK_SIZE,
            "chunker_version": CHUNKER_VERSION,
            "index_version": index_state.version,
        },
        # 快照覆盖率放在顶层：页面上要能一眼看出「下面这些数字覆盖了几成文档」，
        # 埋在某个 stage 的 note 里等于没说。
        "coverage": {
            "documents": len(active_docs),
            "with_snapshot": parsed,
            "missing": parse_den - parsed,
        },
        "generated_at": _now(),
    }


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def document_detail(row: Any, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """单篇的加工明细。老文档没有快照时按现有切片**现算**一份，
    不再依赖当初入库时的那份记录——这样十几份历史文档也能看。"""
    snapshot = snapshot_of(getattr(row, "metadata_json", None) or {})
    sections = {(chunk.get("section") or "") for chunk in chunks}
    lengths = [len(str(chunk.get("content") or "")) for chunk in chunks]

    derived = {
        "structure": {"sections": len(sections - {""}) or len(sections)},
        "chunk": {
            "chunks": len(chunks),
            "chars": sum(lengths),
            "max_chars": max(lengths) if lengths else 0,
        },
    }

    merged = {**derived, **snapshot}

    return {
        "snapshot": merged,
        "derived": not snapshot,
        "tags": tagging.analyze(
            str(getattr(row, "title", "") or ""),
            "\n".join(str(chunk.get("content") or "") for chunk in chunks[:2]),
            filename=getattr(row, "filename", None),
        ).to_dict(),
    }
