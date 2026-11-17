# tests/unit/test_knowledge_tagging.py
"""元数据打标：标准号、年份、多版本归并。

这里钉的是三件容易错的事：

1. ``GB/T 3836.1-2021`` 里的 ``-2021`` 是**年份**，不是编号的一部分。
   早一版正则把它算进编号，导致 ``year`` 永远为空、``standard_base``
   带着年份，于是「同一标准的两个版本」永远归并不到一起——而多版本并存
   正是老化检测最要紧的一条判据。
2. 正文里引用的标准号不能当自己的编号：一份规程会引用七八份国标。
3. 只认标题里没有标准号时才回退到正文，且要求正文那处带年份。
"""

from __future__ import annotations

from app.knowledge import tagging


def test_year_is_split_from_number():
    tags = tagging.analyze("GB/T 3836.1-2021 爆炸性环境 第1部分：设备 通用要求")

    assert tags.standard == "GB/T 3836.1-2021"
    # 归并键不带年份：两版靠它认亲。
    assert tags.standard_base == "GB/T 3836.1"
    assert tags.year == 2021


def test_two_versions_share_the_same_base():
    old = tagging.analyze("GB 27791-2011 城镇燃气调压器")
    new = tagging.analyze("GB 27791-2020 城镇燃气调压器")

    assert old.standard_base == new.standard_base == "GB 27791"
    assert (old.year, new.year) == (2011, 2020)


def test_fullwidth_and_missing_space_are_normalized():
    tags = tagging.analyze("GB/T3836.4—2021 由附录补充")

    assert tags.standard == "GB/T 3836.4-2021"
    assert tags.year == 2021


def test_group_standard_prefix():
    tags = tagging.analyze("T/CGAS 007-2019 城镇燃气调压箱")

    assert tags.standard == "T/CGAS 007-2019"
    assert tags.standard_base == "T/CGAS 007"


def test_doctype_and_topics_from_title():
    tags = tagging.analyze("调压站巡检作业指导书")

    assert tags.doctype == "指导书"
    assert tags.standard == ""
    assert "调压" in tags.topics
    assert "巡检" in tags.topics


def test_body_standard_only_when_title_has_none_and_year_present():
    title_only = tagging.analyze("爆炸性环境 通用要求")
    assert title_only.standard == ""

    with_body = tagging.analyze(
        "爆炸性环境 通用要求",
        "本文件按照 GB/T 1.1-2020 给出的规则起草。",
    )
    assert with_body.standard == "GB/T 1.1-2020"


def test_body_reference_without_year_is_ignored():
    tags = tagging.analyze("调压箱维护手册", "参见 GB 50028 的相关规定。")

    assert tags.standard == ""


def test_tags_are_deduped_and_bounded():
    tags = tagging.analyze("GB 27791-2020 城镇燃气调压器 调压 调压 阀门 泄漏 报警 防爆 压力")

    assert tags.tags[0] == "GB 27791-2020"
    assert len(tags.tags) == len(set(tags.tags))
    assert len(tags.tags) <= tagging.MAX_TAGS


def test_shorthand_tcgas_is_recognised():
    """`TCGAS 027-2023` 与 `T/CGAS 027-2023` 是同一标准。

    封面写规范写法、文件名写无斜杠写法的情形都真实存在。不归一，
    `standard_base` 就是 `TCGAS 027` 与 `T/CGAS 027` 两个值，
    同标准的两个版本永远归并不上，`superseded` 检测直接失效。
    """
    shorthand = tagging.analyze("TCGAS 027-2023城镇燃气智能调压箱技术规范")
    canonical = tagging.analyze("T/CGAS 027-2023 城镇燃气智能调压箱")

    assert shorthand.standard == canonical.standard == "T/CGAS 027-2023"
    assert shorthand.standard_base == canonical.standard_base == "T/CGAS 027"
