"""探测脚本的判据测试。

重点不是跑通网络，而是钉住三态语义——「空 / 扣 / 中」混在一起之后，
这份清单就会得出「缺召回能力」的错误结论（实际情况是缺语料）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.probe_knowledge_gaps import (  # noqa: E402
    ProbeResult,
    load_cases,
    summarize,
)


def _case(**kwargs) -> ProbeResult:
    base = {"case_id": "x", "category": "泄漏与隐患处置", "query": "q", "found": False, "count": 0}
    base.update(kwargs)
    return ProbeResult(**base)


def test_kind_separates_zero_recall_from_dropped():
    """召回 0 与「有召回但全被剔除」是两种缺口，不能并成一类。"""
    assert _case(recall=0, count=0).kind == "empty"
    assert _case(recall=12, count=0).kind == "dropped"
    assert _case(recall=12, count=2, found=True).kind == "hit"


def test_error_does_not_count_as_empty():
    """接口报错不能被算成「语料没有」——那会把故障导成补资料的任务。"""
    item = _case(recall=0, count=0, error="ConnectError: refused")

    assert item.kind == "error"
    assert item.covered is False


def test_covered_requires_citations():
    assert _case(recall=12, count=0, found=False).covered is False
    assert _case(recall=12, count=1, found=True).covered is True


def test_summarize_counts_three_states_per_category():
    results = [
        _case(case_id="a", recall=0, count=0),
        _case(case_id="b", recall=12, count=0),
        _case(case_id="c", recall=12, count=1, found=True),
        _case(case_id="d", category="故障判断与处理", recall=5, count=0),
    ]

    summary = summarize(results)

    assert summary["cases"] == 4
    assert summary["empty"] == 1
    assert summary["dropped"] == 2
    assert summary["hit"] == 1
    assert summary["covered"] == 1
    assert summary["by_category"]["泄漏与隐患处置"]["missed"] == ["a", "b"]
    assert summary["by_category"]["故障判断与处理"]["total"] == 1


def test_shipped_fixture_is_well_formed():
    """随包发布的探测集必须能加载，且每条都有 id/category/query。"""
    cases = load_cases(REPO_ROOT / "tests/fixtures/knowledge_gap_probe.json")

    assert len(cases) >= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(case["category"] for case in cases)


def test_load_cases_rejects_missing_field(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"cases": [{"id": "a", "query": "q"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="category"):
        load_cases(path)
