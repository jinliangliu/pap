# app/knowledge/retriever.py
"""知识检索。

顺序必须是：权限过滤 → 向量/关键词检索 → 排序 → 返回允许展示的片段与引用。
严禁“先检索全部资料再让模型不要引用未授权内容”——那时未授权内容已经进入上下文。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from app.core.config import settings
from app.knowledge.synonyms import expand_query

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Citation:
    document_id: str
    chunk_id: str
    title: str
    section: str
    updated_at: str
    source_path: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STOPWORDS = frozenset(
    """
    的 地 得 了 着 过 是 在 我 你 他 她 它 我们 你们 他们 咱们
    这 那 这个 那个 这些 那些 这里 那里 一个 一下 一些 一点 一样
    有 没 没有 和 与 或 或者 也 都 就 只 而 而且 及 以及 等 等等
    被 把 让 给 对 从 向 于 以 之 其 为 为了 关于
    呢 吗 吧 啊 呀 哦 嗯 么 要 可以 需要 进行 通过
    如果 然后 所以 因为 但是 不过 虽然
    什么 怎么 怎样 如何 为什么 哪些 哪个 多少 是否 有没有 什么样
    the a an of to in for on and or is are was were be been it this that
    with as at by from
    """.split()
)


def tokenize(text: str) -> list[str]:
    """中文分词，剔除虚词与标点。

    停用词必须**索引与查询两侧同时过滤**，不能只在查询侧删：
    语料里留着「的」这类词会把 avgdl 撑大，BM25 的长度惩罚就跟着算错，
    而且虚词挨着 `epsilon` 拿到正权重后照样能挤进前几。

    只收「单看几乎不携带信息」的词。像「前 / 后 / 上 / 下」这种看似虚、
    实际表示时间与位置关系的词不能删——「停机前」和「停机后」是两条规程。
    """
    tokens: list[str] = []

    for raw in jieba.lcut(text.lower()):
        token = raw.strip()

        # 纯标点（，。、）不参与匹配：它们出现在几乎每个切片里，
        # 只会稀释分数，还会让「查询词与切片有交集」这个前提被一个句号满足。
        # 用「含字母或数字」而不是 isalnum()，否则 2.5、k1=1.5 这类数值会被误删。
        if not token or token in STOPWORDS:
            continue
        if not any(char.isalnum() for char in token):
            continue

        tokens.append(token)

    return tokens


def index_tokens(
    record: dict[str, Any],
    *,
    title_boost: int = 0,
    section_boost: int = 0,
) -> list[str]:
    """构造 BM25 的索引词表：正文 + 标题 / section 等结构化字段。

    原先只索引 ``text``。标题行虽然留在 Markdown 正文里，但 PDF / Word / HTML
    抽出的纯文本没有标题，``section`` 只是「第 N 段」；文件名（``title``）
    里的「审批策略」「平台能力说明」这类词更是一次都没进过索引。
    结果是「查询词只出现在标题里」与「正文零交集」完全等价，白白丢召回。

    字段加权用**重复词项**近似（BM25 的 tf 提升，与 ES 的 copy_to + boost
    同类）。不建第二套 BM25 实例：小语料下另一套索引的 IDF 会整体为负，
    加权只会把噪声放大。``0`` 表示完全不索引该字段，用于 A/B 对照。
    """
    tokens = tokenize(str(record.get("text") or ""))
    tokens.extend(tokenize(str(record.get("section") or "")) * section_boost)
    tokens.extend(tokenize(str(record.get("title") or "")) * title_boost)

    return tokens


def allowed_scopes_for(scopes: Iterable[str]) -> set[str]:
    """
    由调用方已认证的 scope 推断可访问的数据分级。

    public 以外的分级必须显式授予对应 scope；
    高敏感数据不入库，因此这里不存在对应分级。
    """
    granted = set(scopes)
    allowed: set[str] = set()

    if "knowledge.public" in granted or "knowledge.read" in granted:
        allowed.add("public")
    if "knowledge.internal" in granted or "knowledge.read" in granted:
        allowed.add("internal")
    if "knowledge.confidential" in granted:
        allowed.add("confidential")

    return allowed


def _is_visible_for(
    record: dict[str, Any],
    *,
    roles: set[str],
    user_id: str,
) -> bool:
    """
    单条切片的 ACL。

    顺序很重要：这是检索**之前**的过滤，未授权内容不会进入 BM25 候选，
    更不会进入模型上下文。不要改成「先检索再让模型忽略」。
    """
    visibility = str(record.get("visibility") or "tenant_shared")

    if visibility == "user_private":
        owner = str(record.get("owner_user_id") or "")

        if not user_id or owner != user_id:
            return False

    required_roles = record.get("allowed_roles") or []

    # 空列表 = 租户内公开（仍受 access_scope 限制）。
    if required_roles and not set(required_roles) & roles:
        return False

    return True


class KnowledgeRetriever:
    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        title_boost: int | None = None,
        section_boost: int | None = None,
    ) -> None:
        self._records = records
        self._title_boost = settings.knowledge_title_boost if title_boost is None else title_boost
        self._section_boost = (
            settings.knowledge_section_boost if section_boost is None else section_boost
        )
        self._tokens = [
            index_tokens(
                record,
                title_boost=self._title_boost,
                section_boost=self._section_boost,
            )
            for record in records
        ]
        # epsilon 把出现在全部文档中的词项负 IDF 抬到正数，
        # 否则小语料下所有相关文档都会被 min_score 过滤掉。
        self._bm25 = BM25Okapi(self._tokens, epsilon=0.25) if self._tokens else None

    def __len__(self) -> int:
        return len(self._records)

    @classmethod
    def from_store(cls, path: str | None = None) -> KnowledgeRetriever:
        target = Path(path or settings.knowledge_store_file)

        if not target.exists():
            raise FileNotFoundError(
                f"未找到索引文件：{target}。请先执行 `uv run python -m scripts.ingest_knowledge`。"
            )

        payload = json.loads(target.read_text(encoding="utf-8"))

        if not isinstance(payload, list):
            raise ValueError("chunks.json 格式不正确：顶层必须是列表。")

        return cls(payload)

    def search(
        self,
        *,
        query: str,
        allowed_scopes: set[str],
        roles: Iterable[str] = (),
        user_id: str = "",
        top_k: int | None = None,
        min_score: float | None = None,
        min_relative_score: float | None = None,
        query_expansion: bool | None = None,
    ) -> tuple[list[dict[str, Any]], list[Citation]]:
        if self._bm25 is None or not allowed_scopes:
            return [], []

        expansion = (
            settings.knowledge_query_expansion_enabled
            if query_expansion is None
            else query_expansion
        )
        query_tokens = tokenize(expand_query(query) if expansion else query)

        if not query_tokens:
            return [], []

        top_k = top_k or settings.knowledge_top_k
        min_score = settings.knowledge_min_score if min_score is None else min_score
        min_relative_score = (
            settings.knowledge_min_relative_score
            if min_relative_score is None
            else min_relative_score
        )

        query_set = set(query_tokens)
        roles_set = {role for role in roles if role}
        scores = self._bm25.get_scores(query_tokens)

        # 权限过滤 + 关键词重叠判断都在排序之前完成。
        # 小语料下 BM25 可能给出负分（IDF 为负），因此以“是否命中查询词”
        # 作为相关性前提，BM25 只负责排序。
        # 词表含标题 / section 的加权词项，所以「只命中标题」也算有交集；
        # 将来若接入向量通道，那条通道**不能**复用这个交集前提，否则
        # 零字面交集的语义相关片段还是进不来，等于白接。
        candidates: list[tuple[int, float]] = []
        for index, record in enumerate(self._records):
            if record.get("access_scope", "internal") not in allowed_scopes:
                continue
            if not _is_visible_for(record, roles=roles_set, user_id=user_id):
                continue
            if not query_set.intersection(self._tokens[index]):
                continue

            score = float(scores[index])
            # 阈值只对正分生效。BM25 的 IDF 在极小语料下会整体为负
            # （词出现在全部文档里时 IDF < 0，`epsilon` 也抬不回正数），
            # 这时分数只代表相对排序、没有绝对意义。拿 min_score 去卡，
            # 会把「唯一一份资料」判成不相关——新租户传完第一篇就搜不到，
            # 看起来像上传失败。
            # 相关性前提已经由上面的「查询词必须与切片有交集」保证了。
            if min_score > 0 and score > 0 and score < min_score:
                continue

            candidates.append((index, score))

        if not candidates:
            return [], []

        ranked = sorted(candidates, key=lambda item: item[1], reverse=True)

        # 相对阈值：低于第一名这个比例的候选直接丢掉。
        # BM25 分数没有绝对量纲——7 个切片时 0.4 可能毫不想关，700 个切片时
        # 0.4 可能是最相关的一条。所以只在「第一名是正分」时按比例裁，
        # 负分语料（小样本下 IDF 全负）维持原样，交给上面的交集前提把关。
        if min_relative_score > 0 and ranked and ranked[0][1] > 0:
            floor = ranked[0][1] * min_relative_score
            ranked = [item for item in ranked if item[1] >= floor]

        ranked = ranked[:top_k]

        documents: list[dict[str, Any]] = []
        citations: list[Citation] = []

        for index, score in ranked:
            record = self._records[index]
            normalized = round(max(score, 0.0), 4)

            documents.append({**record, "score": normalized})
            citations.append(
                Citation(
                    document_id=str(record.get("document_id", "")),
                    chunk_id=str(record.get("chunk_id", "")),
                    title=str(record.get("title", "")),
                    section=str(record.get("section", "")),
                    updated_at=str(record.get("updated_at", "")),
                    source_path=str(record.get("source_path", "")),
                    score=normalized,
                )
            )

        return documents, citations
