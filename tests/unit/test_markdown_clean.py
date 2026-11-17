# tests/unit/test_markdown_clean.py
"""OCR 产物的形态清洗：HTML 表 → pipe 表。

这套用例守的是一份真实文档的教训：GB/T 3836.4—2021 的 PaddleOCR 产物
56.8 万字符里 77% 是 `<td style='word-wrap: break-word'>` 这类标签，
原样入库切出 973 片（超上限被拒），清洗后是 287 片。所以「清洗有没有生效」
不是美观问题，而是「这份资料能不能进库、进去之后检索得到吗」的问题。
"""

from __future__ import annotations

from app.knowledge import ingest
from app.knowledge.markdown_clean import clean_ocr_markdown, looks_taggy, normalize

TABLE_MD = """# 5.1 通则

表 1 的正文在这里。

<table border=1 style='margin: auto; word-wrap: break-word;'>
<tr><td rowspan="2">项目</td><td colspan="2">限值</td></tr>
<tr><td>电压</td><td>1 000 V</td></tr>
</table>

表 1 调压管道结构代号
"""


def test_table_becomes_pipe_table():
    text, stats = clean_ocr_markdown(TABLE_MD)

    assert "<table" not in text and "<td" not in text
    # 合并单元格要摊平：`限值` 跨两列，第一列是它、第二列留空，
    # 下一行的 `电压 / 1 000 V` 才对得上位置。
    # （空列在行内会被压成 `| |`，这是 normalize 压空白的副作用，仍是空单元格。）
    assert "| 项目 | 限值 | |" in text
    assert "| | 电压 | 1 000 V |" in text
    # 题注与正文都不能丢：表格清洗不是删内容。
    assert "表 1 调压管道结构代号" in text
    assert "表 1 的正文在这里。" in text
    assert stats["tables"] == 1
    assert stats["chars_after"] < stats["chars_before"]


def test_every_row_of_a_table_has_the_same_width():
    """列数不齐会让「适用范围」这类表整体错位——读起来像事实写反了。"""
    html = (
        "<table>"
        '<tr><td rowspan="2">项目</td><td colspan="3">适用性</td></tr>'
        "<tr><td>Ⅰ类</td><td>Ⅱ类</td><td>Ⅲ类</td></tr>"
        "<tr><td>5.1</td><td>适用</td><td>排除</td><td>适用</td></tr>"
        "</table>"
    )

    rows = normalize(html).splitlines()
    widths = {row.count("|") for row in rows}

    assert len(widths) == 1, rows


def test_clean_markdown_is_left_alone():
    """干净的 md 不该被这道函数碰：不白跑，也不改它的空行与换行。"""
    text = "# 标题\n\n正文一段。\n\n正文二段。\n"

    cleaned, stats = clean_ocr_markdown(text)

    assert cleaned == text
    assert stats == {}


def test_pipe_inside_cell_is_escaped():
    """单元格里的 `|` 不转义会把列切错，后面每一行都对不上表头。"""
    html = "<table><tr><td>a|b</td><td>c</td></tr></table>"

    assert "a\\|b" in normalize(html)


def test_all_tags_and_no_text_keeps_the_original():
    """只有标签、没有正文时保留原文，而不是返回空串。

    返回空串会让上层报「文档没有可索引的正文」，把「这份 OCR 产物是坏的」
    说成「你传了个空文件」——排查方向整个跑偏。
    """
    html = "<table><tr><td></td></tr></table>"

    cleaned, stats = clean_ocr_markdown(html)

    assert cleaned == html
    assert stats.get("kept_original") == 1


def test_autolink_is_not_mistaken_for_a_tag():
    """`<https://…>` 是 md 语法，不是 OCR 的标签；不能因为含 `<` 就去清洗。"""
    assert looks_taggy("参考 <https://example.com/a> 的说明") is False


def test_ingest_drops_the_tag_noise(monkeypatch):
    """入库路径必须自己清洗——调用方不会记得先跑脚本。

    这是最重要的一条：清洗若只活在页面或导入工具里，目录扫描、附件读取
    这些入口就会把标签原样入库，而它们看起来「都正常」。
    """
    stats: dict[str, int] = {}
    raw = TABLE_MD.encode("utf-8")

    text = ingest.extract_text_from_bytes(raw, "标准.md", stats=stats)

    assert "<td" not in text
    assert stats["tables"] == 1

    doc = ingest.build_document(
        text,
        logical_id="x",
        title="标准",
        source_uri="uploads/标准.md",
        source_type="upload",
        filename="标准.md",
    )

    assert all("<td" not in chunk for chunk in doc.chunks)
