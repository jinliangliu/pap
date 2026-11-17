# tests/unit/test_knowledge_pipeline.py
"""流水线快照。

核心约束：**没有采集过 ≠ 结果是 0**。老文档在 ``metadata_json`` 里没有
``pipeline`` 节，治理页必须能看出「这 3 篇没有快照」，而不是显示
「清洗掉 0 个字」——后者会让人以为清洗没生效，然后去改一段本来没错的代码。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.knowledge import pipeline, tagging
from app.knowledge.ingest import build_document


def _doc(markdown: str, title: str = "调压箱维护手册"):
    return build_document(
        markdown,
        logical_id="doc-1",
        title=title,
        source_uri="uploads/x.md",
        source_type="upload",
        filename="x.md",
        size_bytes=len(markdown.encode("utf-8")),
    )


def test_snapshot_records_each_stage():
    markdown = "# 一、启动前检查\n\n确认阀位反馈。\n\n# 二、泄漏处置\n\n立即通风并报警。\n"
    parsed = _doc(markdown, title="T/CGAS 007-2019 城镇燃气调压箱")

    assert parsed.pipeline["parse"]["extractor"] == "md"
    assert parsed.pipeline["parse"]["chars"] == len(markdown.strip())
    assert parsed.pipeline["structure"]["sections"] == 2
    assert parsed.pipeline["chunk"]["chunks"] == len(parsed.chunks)
    assert parsed.pipeline["metadata"]["standard"] == "T/CGAS 007-2019"
    # 干净的 md 不进清洗分支，这一节就该是空的——不是 0。
    assert parsed.pipeline["clean"] == {}


def test_clean_stats_are_carried_into_snapshot():
    parsed = build_document(
        "# 表格\n\n" + "| a | b |\n| --- | --- |\n| 1 | 2 |\n",
        logical_id="doc-2",
        title="带表的标准",
        source_uri="uploads/y.md",
        source_type="upload",
        filename="y.md",
        pre_stats={"tables": 1, "chars_before": 568000, "chars_after": 140000},
    )

    assert parsed.pipeline["clean"] == {
        "chars_before": 568000,
        "chars_after": 140000,
        "tables": 1,
    }


def test_tags_are_written_to_every_chunk():
    parsed = _doc("# 一节\n\n内容\n\n# 二节\n\n内容\n", title="GB 27791-2020 城镇燃气调压器")

    assert parsed.tags[0] == "GB 27791-2020"


def test_snapshot_of_missing_metadata_is_empty_dict():
    # 返回 {} 而不是填零的结构：调用方据此判断「未采集」。
    assert pipeline.snapshot_of(None) == {}
    assert pipeline.snapshot_of({"chunker_version": 2}) == {}
    assert pipeline.snapshot_of({"pipeline": "oops"}) == {}


def test_document_detail_derives_when_snapshot_missing():
    row = SimpleNamespace(
        title="GB 27791-2011 城镇燃气调压器",
        filename="old.pdf",
        metadata_json={"chunker_version": 2},
    )
    chunks = [
        {"section": "1 范围", "content": "本标准规定了……"},
        {"section": "2 规范性引用", "content": "下列文件……"},
        {"section": "2 规范性引用", "content": "GB/T 1.1-2020"},
    ]

    detail = pipeline.document_detail(row, chunks)

    assert detail["derived"] is True
    assert detail["snapshot"]["structure"]["sections"] == 2
    assert detail["snapshot"]["chunk"]["chunks"] == 3
    # 快照没采过，但标签能现算 —— 老化页正是靠这个覆盖历史文档。
    assert detail["tags"]["standard"] == "GB 27791-2011"


def test_extractor_name_follows_suffix():
    assert pipeline.extractor_of("a.PDF") == "pdf"
    assert pipeline.extractor_of(None, "notes/x.docx") == "docx"
    assert pipeline.extractor_of(None, None) == "unknown"


def test_stage_specs_cover_both_groups():
    ids = [spec.id for spec in pipeline.STAGE_SPECS]

    assert ids == [
        "parse",
        "clean",
        "structure",
        "chunk",
        "metadata",
        "vector",
        "index",
        "graph",
    ]
    assert {spec.group for spec in pipeline.STAGE_SPECS} == {"process", "store"}
    # 未实现的两个阶段必须写明原因与触发条件，否则页面上只剩一个「未启用」。
    for stage_id in ("vector", "graph"):
        assert pipeline.NOT_ENABLED[stage_id]["reason"]
        assert pipeline.NOT_ENABLED[stage_id]["trigger"]


def test_analyze_is_reused_by_both_paths():
    # 入库与读取必须用同一份规则，否则「新传的有标签、老的没有」。
    tags = tagging.analyze("调压箱泄漏处置规程")

    assert "泄漏" in tags.topics
    assert tags.doctype == "规程"
