# tests/unit/test_knowledge_rerank.py
"""语义重筛的单元测试。

全部通过替换 ``_ask_model`` 来模拟模型输出，不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from app.knowledge import rerank as rerank_module
from app.knowledge.rerank import _build_prompt, _parse_keep, rerank
from app.knowledge.retriever import Citation, KnowledgeRetriever
from app.knowledge.service import answer_from_knowledge


def _record(index: int, text: str) -> dict:
    return {
        "chunk_id": f"c{index}-0000",
        "document_id": f"d{index}",
        "chunk_index": 0,
        "text": text,
        "title": f"文档{index}",
        "source_path": f"notes/d{index}.md",
        "section": "第 1 段",
        "updated_at": "2026-11-01T00:00:00+00:00",
        "content_hash": f"sha256:{index}",
        "access_scope": "internal",
    }


def _mock(monkeypatch, output):
    """把模型调用替换成给定输出或异常。"""

    async def fake(prompt: str, *, timeout: float) -> str:
        if isinstance(output, BaseException):
            raise output
        return output

    monkeypatch.setattr(rerank_module, "_ask_model", fake)


def test_parse_keep_handles_fence_duplicates_and_invalid_ids():
    assert _parse_keep('```json\n{"keep": [2, 2, 1]}\n```', 3) == [2, 1]
    # bool 是 int 的子类，True 不能被当成编号 1。
    assert _parse_keep('{"keep": [true, 0, 99, "2", null, 2]}', 3) == [2]
    # 空数组是合法判定（全部无关），不能被当作解析失败。
    assert _parse_keep('{"keep": []}', 3) == []
    assert _parse_keep("综合来看这些片段都不相关", 3) is None
    assert _parse_keep('{"reasons": {"1": "无关"}}', 3) is None


def test_build_prompt_truncates_and_numbers(monkeypatch):
    # 截断仍然要有，否则一个异常长片段就能把 12 条候选的 prompt 顶爆。
    prompt = _build_prompt("阀门停机前确认什么", [_record(1, "阀" * 4000), _record(2, "短路")])

    assert "用户问题：阀门停机前确认什么" in prompt
    assert "[1]" in prompt and "[2]" in prompt
    assert "阀" * (rerank_module.MAX_PROMPT_CHARS + 1) not in prompt


def test_prompt_covers_whole_chunk():
    """整片必须都进 prompt。

    实测翻车：GB 27791 的「过滤精度」在第 507 字，旧上限 500 让模型
    根本没看到答案，于是把召回第一名判成「不相关」。这条测试盯的是
    「重筛上限 ≥ 切片上限」这个耦合，不是某个具体数字。
    """
    from app.knowledge.ingest import CHUNK_SIZE

    assert rerank_module.MAX_PROMPT_CHARS >= CHUNK_SIZE

    tail = "过滤装置的过滤精度不宜低于 50 μm"
    body = "填" * (CHUNK_SIZE - len(tail)) + tail
    prompt = _build_prompt("过滤精度有什么要求", [_record(1, body)])

    assert tail in prompt


def test_prompt_requires_strict_relevance():
    """提示词口径不能被简化回「沾边就保留」，否则线上假阳性会回来。"""
    prompt = rerank_module.SYSTEM_PROMPT

    assert "**2 分**" in prompt
    assert "只把 **2 分**片段" in prompt
    assert "一词多义" in prompt
    assert "不得将失败描述为成功" in prompt


def test_rerank_drops_false_positive_and_preserves_bm25_order(monkeypatch):
    """「描述」撞词的假阳性被剔除，剩下的仍按 BM25 顺序。"""
    _mock(monkeypatch, '{"keep": [2, 3], "reasons": {"1": "只是禁令里的动词"}}')
    docs = [
        _record(1, "工具调用失败时，不得将失败描述为成功。"),
        _record(2, "平台具备知识库检索与工具调用能力。"),
        _record(3, "能力清单：检索、规划、审批。"),
    ]

    result = asyncio.run(rerank(query="描述一下你的能力", documents=docs, top_k=4))

    assert [d["title"] for d in result.documents] == ["文档2", "文档3"]
    assert result.indices == [1, 2]
    assert result.meta.applied is True
    assert result.meta.candidates == 3
    assert result.meta.dropped == 1
    assert result.meta.fallback == ""


def test_rerank_all_irrelevant_returns_empty_without_fallback(monkeypatch):
    """全不相关必须返回空，不能「好心」退回一堆假阳性。"""
    _mock(monkeypatch, '{"keep": []}')

    result = asyncio.run(
        rerank(
            query="描述一下你的能力", documents=[_record(1, "无关"), _record(2, "也无关")], top_k=4
        )
    )

    assert result.documents == []
    assert result.indices == []
    assert result.meta.applied is True
    assert result.meta.dropped == 2


def test_rerank_respects_top_k(monkeypatch):
    _mock(monkeypatch, '{"keep": [1, 2, 3, 4, 5]}')
    docs = [_record(i, f"内容{i}") for i in range(1, 6)]

    result = asyncio.run(rerank(query="内容", documents=docs, top_k=2))

    assert result.indices == [0, 1]
    assert len(result.documents) == 2


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError(), "timeout"),
        (RuntimeError("model_not_configured"), "model_not_configured"),
        (ValueError("bad endpoint"), "model_error: ValueError"),
    ],
)
def test_rerank_falls_back_on_model_failures(monkeypatch, failure, expected):
    _mock(monkeypatch, failure)
    docs = [_record(i, f"内容{i}") for i in range(1, 6)]

    result = asyncio.run(rerank(query="内容", documents=docs, top_k=2))

    assert result.meta.applied is False
    assert result.meta.fallback == expected
    assert result.indices == [0, 1]
    assert [d["title"] for d in result.documents] == ["文档1", "文档2"]


def test_rerank_falls_back_on_unparsable_output(monkeypatch):
    _mock(monkeypatch, "我认为第 1 条相关。")

    result = asyncio.run(rerank(query="内容", documents=[_record(1, "内容")], top_k=4))

    assert result.meta.fallback == "unparsable_output"
    assert len(result.documents) == 1


def test_disabled_rerank_never_calls_model(monkeypatch):
    async def must_not_run(prompt: str, *, timeout: float) -> str:
        raise AssertionError("关闭重筛后不应该调用模型")

    monkeypatch.setattr(rerank_module, "_ask_model", must_not_run)
    monkeypatch.setattr(
        rerank_module,
        "settings",
        SimpleNamespace(knowledge_rerank_enabled=False, knowledge_rerank_timeout_seconds=8.0),
    )

    result = asyncio.run(rerank(query="内容", documents=[_record(1, "内容")], top_k=4))

    assert result.meta.fallback == "disabled"
    assert result.meta.applied is False


def test_service_keeps_citations_aligned_after_rerank(monkeypatch):
    """文档与引用是两条并行列表，重筛后必须同步过滤，不能靠 chunk_id 反查。"""
    _mock(monkeypatch, '{"keep": [2]}')
    records = [
        _record(1, "工具调用失败时，不得将失败描述为成功。"),
        _record(2, "平台具备知识库检索能力。"),
    ]
    retriever = KnowledgeRetriever(records)

    async def fake_retriever(tenant_id):
        return retriever

    monkeypatch.setattr("app.knowledge.service._retriever_for", fake_retriever)

    answer = asyncio.run(
        answer_from_knowledge(
            query="描述一下你的能力", scopes=("knowledge.read",), tenant_id="default"
        )
    )

    assert answer.found
    assert len(answer.documents) == len(answer.citations) == 1
    assert answer.citations[0]["title"] == "文档2"
    assert answer.documents[0]["chunk_id"] == answer.citations[0]["chunk_id"]
    assert answer.rerank["applied"] is True
    assert "以上内容摘自知识库原文" in answer.answer


def test_service_recall_pool_is_wider_than_final(monkeypatch):
    """召回阶段必须关掉两个阈值，否则低分但语义相关的片段根本进不了候选池。"""
    captured = {}

    async def fake(prompt: str, *, timeout: float) -> str:
        captured["prompt"] = prompt
        # 候选池按 BM25 排序，编号不等于原始文档序号；从提示词里找它。
        match = re.search(r"\[(\d+)\] 《文档6》", prompt)
        return json.dumps({"keep": [int(match.group(1))] if match else []})

    monkeypatch.setattr(rerank_module, "_ask_model", fake)
    records = [_record(i, f"阀门停机确认步骤{i}") for i in range(1, 13)]
    # 第 6 条很长，BM25 长度惩罚会把它的分数压到第一名 30% 以下。
    # 它语义上完全相关，正是「召回阶段不能卡阈值」要保住的那类条目。
    records[5]["text"] = "阀门停机确认步骤6。" + "其余都是别的主题的叙述内容。" * 40
    retriever = KnowledgeRetriever(records)

    async def fake_retriever(tenant_id):
        return retriever

    monkeypatch.setattr("app.knowledge.service._retriever_for", fake_retriever)

    answer = asyncio.run(
        answer_from_knowledge(query="阀门停机确认", scopes=("knowledge.read",), tenant_id="default")
    )

    assert "《文档6》" in captured["prompt"], "低分但语义相关的候选应进入重筛输入"
    assert len(answer.documents) == 1
    assert answer.documents[0]["title"] == "文档6"


def test_recall_pool_still_filters_by_access_scope(monkeypatch):
    """扩大召回池不等于放宽权限：未授权切片不能进入模型提示词。"""
    captured = {}

    async def fake(prompt: str, *, timeout: float) -> str:
        captured["prompt"] = prompt
        return '{"keep": [1]}'

    monkeypatch.setattr(rerank_module, "_ask_model", fake)
    records = [
        _record(1, "阀门停机确认步骤，公开内容。"),
        {
            **_record(2, "阀门停机确认步骤，机密薪酬数据 ZZZSECRET。"),
            "access_scope": "confidential",
        },
    ]
    retriever = KnowledgeRetriever(records)

    async def fake_retriever(tenant_id):
        return retriever

    monkeypatch.setattr("app.knowledge.service._retriever_for", fake_retriever)

    asyncio.run(
        answer_from_knowledge(query="阀门停机确认", scopes=("knowledge.read",), tenant_id="default")
    )

    assert "ZZZSECRET" not in captured["prompt"]
    assert "[2]" not in captured["prompt"]


def test_citation_dataclass_fields_unchanged():
    """引用结构被前端跳转依赖，确认没有因重筛改动字段。"""
    citation = Citation(
        document_id="d1",
        chunk_id="c1-0000",
        title="标题",
        section="章节",
        updated_at="2026-11-01T00:00:00+00:00",
        source_path="p.md",
        score=1.0,
    )
    assert citation.to_dict()["chunk_id"] == "c1-0000"


def test_empty_answer_distinguishes_rejected_from_not_recalled(monkeypatch):
    """「查了但都被剔除」和「一条都没召回」不能说同一句话。"""
    from app.agents.prompts import KNOWLEDGE_EMPTY_ANSWER

    _mock(monkeypatch, '{"keep": []}')
    records = [
        _record(1, "工具调用失败时，不得将失败描述为成功。"),
        _record(2, "能力范围仅在技术原则里提了一句。"),
    ]
    retriever = KnowledgeRetriever(records)

    async def fake_retriever(tenant_id):
        return retriever

    monkeypatch.setattr("app.knowledge.service._retriever_for", fake_retriever)

    rejected = asyncio.run(
        answer_from_knowledge(query="描述一下你的能力", scopes=("knowledge.read",), tenant_id="t")
    )
    assert "检索到 2 条候选资料" in rejected.answer
    assert rejected.found is False

    # 查询词与语料完全无交集：BM25 阶段就返回 0 条，重筛没参与。
    no_recall = asyncio.run(
        answer_from_knowledge(query="量子涡旋泵XYZZY", scopes=("knowledge.read",), tenant_id="t")
    )
    assert no_recall.answer == KNOWLEDGE_EMPTY_ANSWER
    assert no_recall.rerank["applied"] is False
