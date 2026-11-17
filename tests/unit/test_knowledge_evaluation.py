# tests/unit/test_knowledge_evaluation.py
"""判分口径。这里每一条都对应一个会让指标说谎的写法。

最要紧的是 ``test_unscored_cases_do_not_drag_metrics_down``：没标期望文档的
用例如果进了分母，「我加了 50 条用例」就会让召回凭空掉一半，然后有人去
「修」一个并不存在的问题。
"""

from __future__ import annotations

import pytest

from app.knowledge import evaluation


def _case(**overrides):
    base = {
        "id": "c1",
        "query": "停机前要确认什么",
        "expect_found": True,
        "expect_document": ["doc-a", "doc-b"],
    }
    base.update(overrides)
    return base


def _hit(document_id: str, section: str = "") -> dict:
    return {"document_id": document_id, "section": section}


def test_recall_counts_matched_labels_not_just_hit():
    # 标注两份、只命中一份 = 召回 0.5。按布尔算会算成 1.0。
    result = evaluation.score_case(_case(), [_hit("doc-a"), _hit("doc-x")])

    assert result.hit is True
    assert result.recall == 0.5
    assert result.matched == ["doc-a"]


def test_precision_penalizes_extra_returns():
    result = evaluation.score_case(
        _case(expect_document=["doc-a"]),
        [_hit("doc-a"), _hit("doc-x"), _hit("doc-y")],
    )

    assert result.precision == pytest.approx(1 / 3)


def test_section_requirement_rejects_wrong_passage():
    case = _case(expect_document=["doc-a"], expect_section="5.1.6 过滤精度")

    wrong = evaluation.score_case(case, [_hit("doc-a", "3 术语和定义")])
    assert wrong.hit is False
    assert wrong.recall == 0.0

    right = evaluation.score_case(case, [_hit("doc-a", "5.1.6 过滤精度")])
    assert right.hit is True


def test_negative_case_hit_is_false_positive():
    case = _case(expect_found=False, expect_document=[])
    result = evaluation.score_case(case, [_hit("doc-a")])

    assert result.is_false_hit is True
    # 负例没有分母，recall/precision 只能是 0，不能编。
    assert result.recall == 0.0
    assert result.precision == 0.0


def test_unscored_cases_do_not_drag_metrics_down():
    scored = evaluation.score_case(_case(), [_hit("doc-a")])
    unscored = evaluation.score_case(
        _case(id="c2", query="未标注的问题", expect_document=[]), [_hit("doc-a")]
    )

    metrics = evaluation.summarize([scored, unscored], k=5)

    assert metrics["cases"] == 2
    assert metrics["scored"] == 1
    assert metrics["unscored"] == 1
    assert metrics["recall"] == 0.5
    # 分母只算参与计分的那一条。
    assert metrics["hit_at_k"] == 1.0


def test_zero_hit_rate_uses_scored_positive_only():
    hit = evaluation.score_case(_case(), [_hit("doc-a")])
    miss = evaluation.score_case(_case(id="c2", expect_document=["doc-z"]), [])
    unscored = evaluation.score_case(_case(id="c3", expect_document=[]), [])

    metrics = evaluation.summarize([hit, miss, unscored], k=5)

    assert metrics["zero_hit_rate"] == 0.5


def test_error_case_is_reported_not_silently_zero():
    failed = evaluation.score_case(
        _case(), [], extra={"error": "RuntimeError: boom"}
    )
    metrics = evaluation.summarize([failed], k=5)

    assert metrics["errors"] == 1
    assert failed.error.startswith("RuntimeError")
