# tests/unit/test_knowledge_eval.py
"""知识检索评测集与指标函数的回归测试。

评测集是「改检索前先量一次」的基准，格式错了或指标算错了，
A/B 对照就只是自欺欺人——这两件事必须钉住。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scripts.eval_knowledge import (
    GOLDEN_PATH,
    load_cases,
    run_local,
    score_case,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_golden_set_is_well_formed():
    cases = load_cases(GOLDEN_PATH)
    ids = [case["id"] for case in cases]
    corpus = REPO_ROOT / "data" / "knowledge"

    assert len(cases) >= 20, "样本太少，Hit@k 会被个别用例带偏"
    assert len(ids) == len(set(ids)), "用例 id 必须唯一，否则报告无法对上号"

    for case in cases:
        relevant = case.get("relevant") or []
        if case["expect_found"]:
            assert relevant, f"{case['id']} 是可回答用例，必须标注 relevant"
        else:
            assert not relevant, f"{case['id']} 是不可回答用例，不该有 relevant"

        for source in relevant:
            assert (corpus / source).exists(), f"{case['id']} 标注的来源不存在：{source}"


def test_score_case_and_summarize():
    cases = [
        {"id": "a", "query": "q1", "expect_found": True, "relevant": ["notes/a.md"]},
        {"id": "b", "query": "q2", "expect_found": True, "relevant": ["notes/b.md"]},
        {"id": "c", "query": "q3", "expect_found": False, "relevant": []},
    ]
    results = [
        score_case(cases[0], ["notes/x.md", "notes/a.md"]),  # 第 2 名才命中
        score_case(cases[1], []),  # 可回答却零召回
        score_case(cases[2], []),  # 正确地拒绝回答
    ]

    assert results[0].reciprocal_rank == 0.5
    assert results[0].hit is True
    assert results[1].hit is False
    assert results[2].is_false_hit is False

    metrics = summarize(results, k=4)
    assert metrics["hit_at_k"] == 0.5
    assert metrics["mrr"] == 0.25
    assert metrics["answerable_zero_hit_rate"] == 0.5
    assert metrics["false_hit_rate"] == 0.0
    assert metrics["cases"] == 3

    # 「库里没有答案却返回了内容」是假阳性，不能算命中。
    with_false_hit = results[:2] + [score_case(cases[2], ["notes/a.md"])]
    assert with_false_hit[2].is_false_hit is True
    assert summarize(with_false_hit, k=4)["false_hit_rate"] == 1.0


def test_load_cases_rejects_unanswerable_with_relevant(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(
        '{"cases":[{"id":"x","query":"q","expect_found":true,"relevant":[]}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relevant"):
        load_cases(path)


def _args(*, no_boost: bool = False, expand: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        k=4,
        knowledge_dir=None,
        no_boost=no_boost,
        expand=expand,
    )


def test_local_baseline_proves_field_boost_effect():
    """字段加权必须有可测量的效果，否则这个改动就只是自我感觉良好。"""
    cases = load_cases(GOLDEN_PATH)
    boosted = {item.case_id: item for item in run_local(_args(), cases)}
    plain = {item.case_id: item for item in run_local(_args(no_boost=True), cases)}

    assert len(boosted) == len(cases)
    assert boosted["filename-only-term"].hit is True
    assert plain["filename-only-term"].found is False


def test_local_baseline_expansion_improves_mrr_without_changing_hit():
    cases = load_cases(GOLDEN_PATH)
    base = run_local(_args(), cases)
    expanded = run_local(_args(expand=True), cases)

    assert summarize(expanded, k=4)["hit_at_k"] == summarize(base, k=4)["hit_at_k"]
    assert summarize(expanded, k=4)["mrr"] >= summarize(base, k=4)["mrr"]


async def test_http_mode_parses_citations_and_rerank(monkeypatch):
    """http 模式的路径拼接、来源与重筛诊断都要能用，不能只在 local 模式下自测。"""
    import httpx

    from scripts import eval_knowledge

    payload = {
        "found": True,
        "answer": "",
        "citations": [{"source_path": "notes/a.md"}],
        "rerank": {"applied": True, "fallback": "", "dropped": 1, "elapsed_ms": 12},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/knowledge/search"
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    cases = [{"id": "a", "query": "q", "expect_found": True, "relevant": ["notes/a.md"]}]
    args = argparse.Namespace(
        base_url="http://127.0.0.1:8000/api/v1",
        subject="test-user",
        tenant="default",
        timeout=5.0,
        k=4,
    )
    results = await eval_knowledge.run_http(args, cases)

    assert len(results) == 1
    assert results[0].hit is True
    assert results[0].rerank["applied"] is True

    metrics = summarize(results, k=4)
    assert metrics["rerank_applied"] == 1
    assert metrics["rerank_dropped"] == 1
    assert metrics["rerank_elapsed_p50_ms"] == 12
