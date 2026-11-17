"""标题归一：OCR 长尾要被清掉，标准号里的斜杠不能被当路径切掉。"""

from __future__ import annotations

from app.knowledge.ingest import build_document
from app.knowledge.titles import MAX_TITLE_CHARS, clean_title


def test_strips_paddleocr_tail():
    assert (
        clean_title("TCGAS 027-2023 城镇燃气调压箱 技术规范.pdf_by_PaddleOCR-VL-1.6")
        == "TCGAS 027-2023 城镇燃气调压箱 技术规范"
    )


def test_strips_tail_after_source_suffix():
    # 原始 JSON 产物：先剥 .json 才看得见长尾。
    assert (
        clean_title("GB 27791-2011 城镇燃气调压箱.pdf_by_PaddleOCR-VL-1.6.json")
        == "GB 27791-2011 城镇燃气调压箱"
    )
    assert clean_title("某规范_by_PaddleOCR-VL.md") == "某规范"


def test_keeps_slash_in_standard_number():
    """GB/T 里的斜杠是编号的一部分。

    这里曾经按 ``/`` 取末段当文件名，于是 ``GB/T 3836.1-2021`` 变成
    ``T 3836.1-2021``——标准号一坏，同标准的版本归并也跟着失效。
    """
    assert clean_title("GB/T 3836.1-2021 爆炸性环境 第1部分.pdf") == "GB/T 3836.1-2021 爆炸性环境 第1部分"
    assert clean_title("T/CGAS 007-2019 城镇燃气调压箱") == "T/CGAS 007-2019 城镇燃气调压箱"


def test_plain_names_untouched():
    assert clean_title("调压站巡检作业指导书.md") == "调压站巡检作业指导书"
    assert clean_title("GB 27791-2020 城镇燃气调压器") == "GB 27791-2020 城镇燃气调压器"


def test_fallback_when_value_empty():
    assert clean_title(None, fallback="a.txt") == "a"
    assert clean_title("   ", fallback="b.md") == "b"
    # 全空返回空串，由调用方决定再怎么兜底（build_document 会用 logical_id）。
    assert clean_title("", fallback="") == ""


def test_truncates():
    assert len(clean_title("标" * 400)) == MAX_TITLE_CHARS


def test_build_document_cleans_title_from_filename():
    """没有显式 title 时，兜底标题也走同一份规则。"""
    parsed = build_document(
        "一、启动前检查\n\n确认阀位反馈。\n",
        logical_id="x",
        title="",
        source_uri="uploads/a.md",
        source_type="file",
        # 真实文件名里不可能有 `/`（`GB/T` 那种写法只出现在标题里），
        # 这里用 OCR 产物的实际命名。
        filename="CGAS 027-2023 技术规范.pdf_by_PaddleOCR-VL-1.6.md",
    )

    assert parsed.title == "CGAS 027-2023 技术规范"


def test_standard_marks_are_normalized():
    """标准号的书写变体要归一到与标签同一个串。

    国标排版用长横（`GB/T 41315—2022`），文件系统又不允许斜杠、导出工具
    把它换成了除号（`GB∕T`）。不归一的话，标题里是一个串、`tagging` 抽出的
    标签是另一个，检索按标题加权时用户搜哪一个都可能漏。
    """
    assert (
        clean_title("GB/T 41315—2022 城镇燃气输配系统用安全切断阀")
        == "GB/T 41315-2022 城镇燃气输配系统用安全切断阀"
    )
    assert (
        clean_title("GB∕T 41315-2022 城镇燃气输配系统用安全切断阀")
        == "GB/T 41315-2022 城镇燃气输配系统用安全切断阀"
    )
    assert clean_title("GB/T 1.1－2020 标准化工作导则") == "GB/T 1.1-2020 标准化工作导则"
    assert clean_title("GB 50028–2006 城镇燃气设计规范") == "GB 50028-2006 城镇燃气设计规范"


def test_year_range_is_not_a_standard_number():
    """「2020—2021」不是标准号，长横必须留着。

    规则只认「字母前缀 + 数字编号 + 年份」，放宽成「数字—数字」会把
    标题里的年份区间、日期范围一起改掉。
    """
    assert clean_title("2020—2021 年度报告") == "2020—2021 年度报告"
    assert clean_title("第 3—5 章 调压设备") == "第 3—5 章 调压设备"


def test_normalized_title_matches_tagging_standard():
    """标题归一后，与打标抽出的标准号应当逐字相同。"""
    parsed = build_document(
        "# 5 要求\n\n切断阀应在超压时自动关闭。\n",
        logical_id="std-marks",
        title="GB/T 41315—2022 城镇燃气输配系统用安全切断阀",
        source_uri="uploads/a.md",
        source_type="file",
        filename="a.md",
    )

    assert parsed.title.startswith("GB/T 41315-2022")
    assert parsed.tags
    assert "GB/T 41315-2022" in parsed.tags
