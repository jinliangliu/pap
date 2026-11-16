# tests/unit/test_ocr_json_to_md.py
"""OCR JSON → Markdown 的转换规则。

这些断言盯的都是**会改变检索结果**的行为，不是格式洁癖：
水印不去掉会变成全库高频词，表格标签不去掉会把 style 属性切成 token，
重复页不去掉会让同一段正文占掉两个引用位。
"""

from __future__ import annotations

from scripts.ocr_json_to_md import (
    boilerplate_lines,
    convert,
    normalize_markdown,
    table_to_markdown,
)


def _page(text: str, *, header: str = "", footer: str = "", blocks=None) -> dict:
    parsing = list(blocks or [])

    if header:
        parsing.append({"block_label": "header", "block_content": header})
    if footer:
        parsing.append({"block_label": "footer", "block_content": footer})

    return {
        "markdown": {"text": text, "images": {}},
        "prunedResult": {"parsing_res_list": parsing},
    }


def test_table_becomes_markdown_pipe_table():
    html = (
        "<table border=1 style='word-wrap: break-word;'><tr>"
        "<td style='text-align: center;'>代号</td><td>A</td></tr>"
        "<tr><td>管道结构</td><td>1+0</td></tr></table>"
    )

    parsed = table_to_markdown(html)

    assert parsed.splitlines()[0] == "| 代号 | A |"
    assert parsed.splitlines()[1] == "| --- | --- |"
    assert "管道结构" in parsed
    # 属性名不该漏进正文：它们是 token 噪声。
    assert "word-wrap" not in parsed and "center" not in parsed


def test_repeated_header_footer_is_dropped_but_content_survives():
    pages = [
        _page("正文甲", header="GB 27791—2011", footer="标准资料网 WWW.PV265.COM"),
        _page("正文乙", header="GB 27791—2011", footer="标准资料网 WWW.PV265.COM"),
    ]

    markdown, stats = convert(pages)

    assert "正文甲" in markdown and "正文乙" in markdown
    assert "WWW.PV265" not in markdown
    assert "GB 27791—2011" not in markdown
    assert len(stats["boilerplate"]) == 2


def test_single_page_header_is_kept():
    """只出现一次的页眉不能一刀切。

    `markdown.text` 本身通常已经不含 header/footer 块（那边标了 ignore），
    但水印偶尔会漏进正文——漏一次的就是正文，漏到每页的才是水印。
    """
    pages = [_page("唯一的一次\n正文", header="唯一的一次")]

    assert boilerplate_lines(pages) == set()
    markdown, _ = convert(pages)
    assert "唯一的一次" in markdown


def test_duplicate_page_is_dropped():
    body = "5.1 一般要求\n管道布置应结构合理。"
    pages = [_page(body), _page("另一页"), _page(body)]

    markdown, stats = convert(pages)

    assert stats["duplicate_pages"] == 1
    assert markdown.count("管道布置应结构合理") == 1


def test_images_dropped_but_caption_kept():
    text = (
        '<div style="text-align: center;"><div>表1 调压管道结构代号</div></div>\n\n'
        '<div><img src="imgs/a.jpg" alt="Image" width="43%" /></div>'
    )

    markdown, _ = convert([_page(text)])

    assert "表1 调压管道结构代号" in markdown
    assert "img" not in markdown and "<div" not in markdown


def test_headings_survive_because_sections_come_from_them():
    text = "## 5 结构要求\n\n### 5.1 一般要求\n\n5.1.6 过滤精度不宜低于 50 μm。"

    markdown, stats = convert([_page(text)])

    assert "## 5 结构要求" in markdown
    assert "### 5.1 一般要求" in markdown
    assert stats["headings"] == 2


def test_page_without_markdown_falls_back_to_blocks():
    page = {
        "prunedResult": {
            "parsing_res_list": [
                {"block_label": "text", "block_content": "后半段", "block_order": 2},
                {"block_label": "text", "block_content": "前半段", "block_order": 1},
                # 页码不进正文
                {"block_label": "number", "block_content": "12", "block_order": 3},
            ]
        }
    }

    markdown, _ = convert([page])

    assert markdown.index("前半段") < markdown.index("后半段")
    assert "12" not in markdown


def test_markdown_input_gets_its_html_tables_converted():
    """PaddleOCR-VL 也会直接吐 `.md`，里面的表格仍是 HTML。

    表格是标准里信息密度最高的部分：原样入库，每条引用都拖着一串
    `<td style='text-align: center; word-wrap: break-word;'>`，读起来全是噪声。
    """
    source = (
        "### 5.2 计量系统\n\n"
        "<table border=1 style='margin: auto; word-wrap: break-word;'>"
        "<tr><td style='text-align: center;'>等级</td><td>准确度</td></tr>"
        "<tr><td>A</td><td>1%</td></tr></table>\n\n"
        "<div style=\"text-align: center;\">图 1 示意图</div>\n"
    )

    markdown = normalize_markdown(source)

    assert "| 等级 | 准确度 |" in markdown
    assert "| A | 1% |" in markdown
    # 包裹层只去标签、留文字：题注常常就在 div 里，删了会丢图号。
    assert "图 1 示意图" in markdown
    assert "<" not in markdown


def test_markdown_input_keeps_less_than_in_formulas():
    """公式里的 `1000< q_n` 是真小于号，不能被当成标签残片清掉。"""
    source = "| 设计能力 | 1 000< $ q_n $≤10 000 |\n"

    assert "1 000< $ q_n $" in normalize_markdown(source)
