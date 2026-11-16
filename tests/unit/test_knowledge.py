# tests/unit/test_knowledge.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.knowledge.ingest import build_index, is_excluded
from app.knowledge.retriever import KnowledgeRetriever, allowed_scopes_for


def test_sensitive_files_are_excluded():
    assert is_excluded(Path(".env")) is True
    assert is_excluded(Path(".env.save")) is True
    assert is_excluded(Path("secrets/id_rsa")) is True
    assert is_excluded(Path("certs/server.pem")) is True
    assert is_excluded(Path("node_modules/pkg/readme.md")) is True
    assert is_excluded(Path("__pycache__/x.md")) is True
    assert is_excluded(Path("notes/architecture.md")) is False


def test_index_skips_sensitive_files_and_records_metadata(tmp_path: Path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "architecture.md").write_text(
        "# 架构\n\n工具网关负责权限与审批。\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")

    records, report = build_index(str(tmp_path))

    assert report.documents == 1
    assert report.chunks >= 1

    record = records[0]
    assert record["source_path"] == "notes/architecture.md"
    assert record["content_hash"].startswith("sha256:")
    assert record["access_scope"] == "internal"
    assert record["updated_at"]

    skipped = " ".join(report.skipped)
    assert ".env" in skipped
    assert "id_rsa" in skipped


def test_retrieval_filters_before_scoring(tmp_path: Path):
    records = [
        {
            "chunk_id": "public-0000",
            "document_id": "public",
            "chunk_index": 0,
            "text": "场站压力阈值规范",
            "title": "公开规范",
            "source_path": "public/spec.md",
            "section": "第 1 段",
            "updated_at": "2026-11-01T00:00:00+00:00",
            "content_hash": "sha256:x",
            "access_scope": "public",
        },
        {
            "chunk_id": "conf-0000",
            "document_id": "conf",
            "chunk_index": 0,
            "text": "场站压力阈值机密处置流程",
            "title": "机密流程",
            "source_path": "confidential/flow.md",
            "section": "第 1 段",
            "updated_at": "2026-11-01T00:00:00+00:00",
            "content_hash": "sha256:y",
            "access_scope": "confidential",
        },
    ]

    retriever = KnowledgeRetriever(records)

    documents, citations = retriever.search(
        query="场站压力阈值",
        allowed_scopes={"public", "internal"},
        min_score=0.0,
    )

    titles = {document["title"] for document in documents}
    assert "机密流程" not in titles
    assert citations and citations[0].source_path.startswith("public/")


def test_allowed_scopes_mapping():
    assert allowed_scopes_for(()) == set()
    assert allowed_scopes_for(("knowledge.read",)) == {"public", "internal"}
    assert "confidential" in allowed_scopes_for(("knowledge.confidential",))


def test_store_roundtrip(tmp_path: Path):
    path = tmp_path / "chunks.json"
    path.write_text(json.dumps([]), encoding="utf-8")

    retriever = KnowledgeRetriever.from_store(str(path))
    assert len(retriever) == 0
    assert retriever.search(query="任意", allowed_scopes={"public"}) == ([], [])


def test_single_document_corpus_still_returns_hits():
    """回归：语料只有一份文档时，BM25 的 IDF 全为负，min_score 会误杀。

    新租户第一次上传资料就撞上这个：传成功、列表里有、搜不到，
    看起来像上传没生效。相关性前提由「查询词必须与切片有交集」保证，
    阈值只该对正分生效。
    """
    records = [
        {
            "chunk_id": "only-0000",
            "document_id": "only",
            "chunk_index": 0,
            "text": "# 阀门停机规程\n\n停机前必须确认阀位反馈与下游压力。",
            "title": "停机规程",
            "source_path": "uploads/停机规程.md",
            "section": "第 1 段",
            "updated_at": "2026-11-01T00:00:00+00:00",
            "content_hash": "sha256:z",
            "access_scope": "internal",
        }
    ]

    documents, citations = KnowledgeRetriever(records).search(
        query="停机前要确认什么",
        allowed_scopes={"internal"},
        # 不显式传 min_score，走默认配置——缺陷正是在默认配置下出现的。
    )

    assert len(documents) == 1
    assert citations[0].title == "停机规程"


def test_bytes_parsing_matches_file_parsing(tmp_path: Path):
    """CRLF 与 LF 必须解析成同一份正文——「重复上传不重复入库」靠它。

    实测翻车：同一份 27791，一个是从磁盘读的 .md（Windows 写的 CRLF），
    一个是内存里现转的（LF），内容一样、字节不同，于是哈希不同，
    库里多出一份 48 片的副本。
    """
    from app.knowledge.ingest import extract_text, extract_text_from_bytes

    body = "# 标题\n\n正文段落。\n"
    crlf = body.replace("\n", "\r\n")

    assert extract_text_from_bytes(body.encode("utf-8"), "doc.md") == body
    assert extract_text_from_bytes(crlf.encode("utf-8"), "doc.md") == body

    # 写盘时不指定 newline：在 Windows 上落盘就是 CRLF，结果仍要一致。
    path = tmp_path / "doc.md"
    path.write_text(body, encoding="utf-8")

    assert extract_text(path) == body


def test_build_document_rejects_oversized_document(monkeypatch):
    """切片数超上限要明确报错，而不是静默截断。

    静默截掉后半本手册，检索会「看起来能用」但永远查不到后半部分的内容，
    这种缺陷比直接失败难查得多。
    """
    from app.knowledge import ingest

    monkeypatch.setattr(ingest.settings, "knowledge_max_chunks_per_document", 1)

    with pytest.raises(ingest.KnowledgeIngestError, match="超过单篇上限"):
        ingest.build_document(
            # 五段、每段都超过 CHUNK_SIZE，必然切出多片。
            "\n\n".join(["段" * 900 for _ in range(5)]),
            logical_id="big",
            title="大文档",
            source_uri="uploads/big.md",
            source_type="upload",
        )


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


def test_tokenize_drops_stopwords_and_punctuation():
    """停用词与标点不进入分词结果。

    改动前 `jieba.lcut` 的结果原样入索引，「的」这种词在几乎每个切片里
    拿到 `epsilon` 抬出来的正权重，能把一条纯虚词命中的切片送进前四。
    """
    from app.knowledge.retriever import tokenize

    assert tokenize("描述一下你的能力") == ["描述", "能力"]
    assert tokenize("的了吗呢吧") == []
    assert "，" not in tokenize("阀门，检修。")
    # 数字不能被当标点误删：压力值、版本号都是检索目标。
    assert "2.31" in tokenize("压力 2.31kPa")


def test_stopword_only_chunk_never_matches():
    """整段都是虚词的切片不该被任何查询命中。

    `min_relative_score=0` 是为了关掉阈值只验证停用词过滤本身——
    阈值会顺手把低分项也裁掉，两件事混在一起就测不出是谁起的作用。
    """
    records = [
        _record(0, "工具调用失败时，不得将失败描述为成功。"),
        _record(1, "的了吗呢这一条只包含虚词的了"),
    ]

    documents, _ = KnowledgeRetriever(records).search(
        query="请描述一下你的能力",
        allowed_scopes={"internal"},
        min_relative_score=0,
    )

    assert [document["title"] for document in documents] == ["文档0"]


def test_relative_threshold_drops_low_score_candidates():
    """绝对阈值拦不住的东西，改用「第一名的一个比例」来拦。

    语料里只提到一次查询词、其余都在讲别的事的切片，BM25 可能给它一个
    接近 0 的分（甚至因负 IDF 被截到 0），但它照样占着 top_k 的名额。
    """
    records = [
        _record(0, "阀门停机确认步骤：先关上游，再确认阀位反馈。"),
        _record(1, "阀门检修记录：更换密封圈，测试压降正常。"),
        _record(2, "场站巡检路线包含三个压力监测点。"),
        _record(3, "调度值班交接班管理规范与记录要求。"),
        _record(4, "应急预案演练每季度组织一次并留存影像。"),
        _record(5, "这是无关内容，只提到一次阀门。" + "其余都是别的主题的叙述内容。" * 12),
    ]

    retriever = KnowledgeRetriever(records)

    wide, _ = retriever.search(
        query="阀门停机确认",
        allowed_scopes={"internal"},
        min_relative_score=0,
    )
    assert len(wide) > 1, "关掉阈值后应该有多条候选，否则这个用例测不到东西"

    narrow, citations = retriever.search(
        query="阀门停机确认",
        allowed_scopes={"internal"},
    )

    assert len(narrow) == 1
    assert citations[0].title == "文档0"
    # 相对阈值只裁尾巴，不能改动排序：第一名必须还是原来那条。
    assert narrow[0]["title"] == wide[0]["title"]


def test_split_document_uses_markdown_headings():
    """引用里显示「技术原则」比「第 1 段」有用得多。"""
    from app.knowledge.ingest import split_document

    sections = split_document(
        "# 项目规划\n\n开头一段说明。\n\n"
        "## 技术原则\n\n先保证正确性，再谈性能。\n\n"
        "## 里程碑\n\n第一阶段做检索。"
    )

    assert [item.section for item in sections] == [
        "项目规划",
        "项目规划 / 技术原则",
        "项目规划 / 里程碑",
    ]
    # 标题行留在正文里：BM25 命中标题词是强相关信号。
    assert "## 技术原则" in sections[1].text
    assert "先保证正确性" in sections[1].text


def test_split_document_falls_back_without_headings():
    """PDF / Word / HTML 抽出的纯文本没有标题，维持原来的「第 N 段」。"""
    from app.knowledge.ingest import split_document

    sections = split_document("第一段讲启动流程。\n\n第二段讲停机流程。")

    assert [item.section for item in sections] == ["第 1 段"]


def test_split_document_numbers_long_sections():
    from app.knowledge.ingest import split_document

    body = "## 超长小节\n\n" + "\n\n".join(["段" * 900 for _ in range(3)])
    sections = split_document(body)

    assert len(sections) > 1
    assert all(item.section.startswith("超长小节（") for item in sections)
    assert "1/" in sections[0].section


def test_split_document_ignores_headings_inside_code_fences():
    """文档里的 shell 示例经常带 # 注释，不能被当成标题切断。"""
    from app.knowledge.ingest import split_document

    text = "# 使用说明\n\n运行脚本：\n\n```bash\n# 这是注释，不是标题\ncd /srv/app\n```\n\n结束。\n"
    sections = split_document(text)

    assert len(sections) == 1
    assert sections[0].section == "使用说明"
    assert "# 这是注释，不是标题" in sections[0].text


def test_build_document_keeps_sections_aligned_with_chunks():
    from app.knowledge.ingest import CHUNKER_VERSION, build_document

    document = build_document(
        "# 手册\n\n## 安装\n\n第一步。\n\n## 卸载\n\n第二步。\n",
        logical_id="manual",
        title="手册",
        source_uri="uploads/manual.md",
        source_type="upload",
    )

    assert len(document.sections) == len(document.chunks) == 3
    assert document.sections[1] == "手册 / 安装"
    assert document.chunker_version == CHUNKER_VERSION


def test_index_tokens_weights_heading_fields():
    """正文之外，标题与 section 的分词按配置重复若干次并入索引。"""
    from app.knowledge.retriever import index_tokens, tokenize

    record = {
        "text": "正文讲压力参数。",
        "title": "approval_policy",
        "section": "审批策略 / 有效期",
    }
    plain = index_tokens(record)
    boosted = index_tokens(record, title_boost=2, section_boost=1)

    assert "approval" not in plain
    assert boosted.count("approval") == 2
    assert all(token in boosted for token in tokenize("审批策略 / 有效期"))
    # 正文仍在，且只出现一次：加权不应把正文词也放大。
    assert boosted.count("压力") == 1


def test_title_boost_recalls_filename_only_terms():
    """回归：查询词只在文件名里、正文一次都没出现时，旧逻辑直接返回 0 条。"""
    records = [
        {
            **_record(0, "写操作必须先生成预览，等待用户确认后执行。"),
            "title": "approval_policy",
            "section": "审批策略",
        },
        _record(1, "其他资料：索引构建命令与目录说明。"),
    ]
    search_kwargs = {
        "query": "approval_policy",
        "allowed_scopes": {"internal"},
        "top_k": 4,
        "min_score": 0,
        "min_relative_score": 0,
    }

    without = KnowledgeRetriever(records, title_boost=0, section_boost=0)
    with_boost = KnowledgeRetriever(records, title_boost=2, section_boost=1)

    assert without.search(**search_kwargs)[1] == []
    _, citations = with_boost.search(**search_kwargs)
    assert [item.source_path for item in citations] == ["notes/d0.md"]


def test_query_expansion_is_query_side_only(monkeypatch):
    """同义词只能改查询侧，索引侧扩词会把 IDF 与 avgdl 全部弄脏。"""
    from app.core.config import settings
    from app.knowledge.synonyms import expand_query

    assert "平台能力" in expand_query("你会做什么")
    assert expand_query("今天天气怎么样") == "今天天气怎么样"

    records = [_record(0, "平台能力清单：知识检索、工具调用、写操作审批。")]
    before = KnowledgeRetriever(records, title_boost=0, section_boost=0)._tokens[0]

    monkeypatch.setattr(settings, "knowledge_query_expansion_enabled", True)
    after = KnowledgeRetriever(records, title_boost=0, section_boost=0)._tokens[0]

    assert before == after


def test_search_query_expansion_recalls_paraphrase():
    """口语化提问与资料零字面交集时，扩展能召回；不扩展时仍应是 0 条。"""
    records = [_record(0, "平台能力清单：知识检索、工具调用、写操作审批。")]
    retriever = KnowledgeRetriever(records, title_boost=0, section_boost=0)
    search_kwargs = {
        "query": "你会做什么",
        "allowed_scopes": {"internal"},
        "top_k": 4,
        "min_score": 0,
        "min_relative_score": 0,
    }

    assert retriever.search(**search_kwargs, query_expansion=False)[1] == []
    _, citations = retriever.search(**search_kwargs, query_expansion=True)
    assert len(citations) == 1
