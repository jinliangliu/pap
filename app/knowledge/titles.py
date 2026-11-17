# app/knowledge/titles.py
"""文档标题的归一化。

标题要进两处：检索的标题加权（命中标题的切片会提分）和页面上的文档列表。
OCR 工具会把处理标记带进文件名——`T/CGAS 027-2023 技术规范.pdf_by_PaddleOCR-VL-1.6`
——这串东西在加权里是纯噪声，在列表里占掉多半个宽度，还让同一份资料换个
OCR 版本就像换了个人。

规则只认固定标记，不做「按后缀猜」：标题里出现 `.pdf_by_` 的真实文档不存在，
但把规则写宽（比如「截掉最后一个 `.pdf`」）会把
`GB 27791-2011 城镇燃气调压箱.pdf` 这种本来就正常的名字截成半截。

三个地方原本各写了一份（上传兜底、控制台导入、`scripts/pap_push.py`），
三份的标记列表还不一样：CLI 会清掉长尾，另两处不清。合并到这里以后，
服务端是唯一权威——即便某个客户端漏了，入库时也会被清掉。
"""

from __future__ import annotations

import re

# 与 upload 的既有取值一致：200 字符足够放下「标准号 + 全称 + 第几部分」。
MAX_TITLE_CHARS = 200

# OCR 工具留下的标记。`.pdf_by_` 覆盖 PaddleOCR-VL 的两种大小写写法，
# `_by_PaddleOCR` 覆盖不带原扩展名的产物。
_OCR_TAIL_MARKERS = (".pdf_by_", ".PDF_by_", ".Pdf_by_", "_by_PaddleOCR")

# 能当「扩展名」剥掉的尾缀。只收知识库真正接受的类型：
# 把 `.json` 也算进来会让「xxx.json 治理记录」这种正经标题被截断。
_STRIP_SUFFIXES = (".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".html", ".htm")

# 斜杠的几种变体。文件系统不允许 `/`，于是导出工具把它换成了除号
# （U+2215）、分数斜杠（U+2044）或全角斜杠（U+FF0F）。不还原的话
# `GB∕T 41315` 和 `GB/T 41315` 是两份看起来一样的标准，标题与标签也对不上。
_SLASH_VARIANTS = str.maketrans({"∕": "/", "⁄": "/", "／": "/"})

# 标准号里「编号—年份」的连接符。国标排版用长横（U+2014），OCR 与
# 手写文件名里还会混进短横、全角减号、水平线。统一成半角连字符：
# `tagging.find_standards` 抽出来的标签本来就归一到半角，标题不归一就会出现
# 「标题 GB/T 41315—2022 / 标签 GB/T 41315-2022」两个串，检索按标题加权时
# 用户搜哪一个都可能漏。
# 只认「字母前缀 + 数字编号 + 四位年份」，所以「2020—2021 年度报告」
# 这种正文区间不会被改。
_YEAR_DASH = re.compile(
    r"(?P<prefix>[A-Z]{1,5}(?:/[A-Z]{1,5})?\s*\d+(?:\.\d+)*)\s*[—–―‒－]\s*"
    r"(?P<year>(?:19|20)\d{2})"
)


def _normalize_marks(text: str) -> str:
    """把标准号的书写变体归一到与标签一致的形式。"""
    return _YEAR_DASH.sub(
        lambda match: f"{match.group('prefix')}-{match.group('year')}",
        text.translate(_SLASH_VARIANTS),
    )


def _strip_once(text: str) -> str:
    """剥一层：先截 OCR 长尾，再剥一个扩展名。都不成立就原样返回。"""
    for marker in _OCR_TAIL_MARKERS:
        head, separator, _tail = text.partition(marker)

        if separator:
            return head.strip()

    lowered = text.lower()

    for suffix in _STRIP_SUFFIXES:
        if lowered.endswith(suffix):
            return text[: -len(suffix)].strip()

    return text


def clean_title(value: str | None, *, fallback: str = "", max_chars: int = MAX_TITLE_CHARS) -> str:
    """把文件名/标题清成可直接展示的样子。

    ``fallback`` 在 ``value`` 清完是空串时使用（例如只给了后缀名为 ``.pdf``
    的文件名）。剥多层是有意的：``规范.pdf_by_PaddleOCR-VL-1.6.md`` 剥掉
    ``.md`` 才看得见长尾，再剥长尾剩下的 ``规范`` 才是标题。

    传进来的应当是**文件名**而不是路径：标准号 ``GB/T 3836.1`` 里的斜杠
    和目录分隔符长得一样，任何按 ``/`` 切分的写法都会把编号切坏。
    """
    for source in (value, fallback):
        # 只处理「名字」，不剥目录。曾经在这里按 `/` 取末段，于是
        # `GB/T 3836.1-2021 …` 被切成 `T 3836.1-2021 …`——标准号里的斜杠
        # 是编号的一部分，不是路径分隔符。调用方传文件名（`Path(x).name`）。
        text = _normalize_marks((source or "").strip())

        # 最多剥三层：`名字.pdf_by_…-1.6.json` 正好两层，留一层余量。
        for _ in range(3):
            stripped = _strip_once(text)

            if stripped == text:
                break

            text = stripped

        if text:
            return text[:max_chars]

    return ""
