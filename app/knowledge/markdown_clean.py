# app/knowledge/markdown_clean.py
"""OCR 产物的 Markdown 形态清洗：HTML 表格 → pipe 表，去掉包裹层标签。

## 为什么入库路径需要这一步

PaddleOCR-VL 有时直接吐 `.md`（而不是 JSON），里面的表格仍是
`<table border=1 style='margin: auto; word-wrap: break-word;'><tr><td rowspan="2">`。
实测 GB/T 3836.4—2021 的这份产物：56.8 万字符里 **77% 是标签**，切出 973 片，
真正有语义的不到三成；`word` / `break` / `center` / `rowspan` 这些属性名还会被
切成 token 喂进 BM25。同一份文档清洗后是 14 万字、287 片、标签归零。

## 这不是格式化

表格里的信息一点没丢，只是换成检索看得懂的形态：`<td>1 000</td>` 进索引是
`1`/`000` 两个噪声词，`| 1 000 |` 进索引才是一个数。所以它属于「把一份资料
变成切片」的范畴，与切片、行尾归一同层——**不是**替用户改内容。

原来的实现只活在 `scripts/ocr_json_to_md.py` 里，入库路径拿不到，于是「脚本
处理过的不脏、直接上传的就脏」。现在两边共用这一份（脚本从这里 import）。

只用标准库：脚本要能在没装 bs4 的机器上跑（服务器上是空 venv）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 表格整块（跨行匹配）。`[\s\S]` 而不是 `.`：OCR 的表格 HTML 一行就是几万字，
# 用 DOTALL 更直观，但 `[\s\S]` 不依赖 flag，抄错的风险小。
_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
# 只剩「像标签」的串：`<td ...>`、`</tr>`、`<span ...>`、`<div ...>`。
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BLANK_RE = re.compile(r"\n{3,}")

# 出现这些子串就认为「这份 md 带着 OCR 的标签味」。不用泛化的「有没有 `<`」：
# 正常 md 里 `<https://…>` 这种自动链接会被 _TAG_RE 误伤，而它不该被清洗。
_TAGGY_MARKERS = ("<table", "<td", "<tr", "<th", "<div", "<span")


class _TableCell:
    __slots__ = ("colspan", "rowspan", "text")

    def __init__(self, text: str, *, colspan: int, rowspan: int) -> None:
        self.text = text
        self.colspan = colspan
        self.rowspan = rowspan


def _span_of(attrs: list[tuple[str, str | None]], name: str) -> int:
    for key, value in attrs:
        if key == name:
            try:
                return max(1, int(value or 1))
            except (TypeError, ValueError):
                return 1
    return 1


class _TableParser(HTMLParser):
    """把 `<table>` 采成「单元格 + 合并信息」的二维数组。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_TableCell]] = []
        self._row: list[_TableCell] | None = None
        self._cell: list[str] | None = None
        self._span: tuple[int, int] = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
            self._span = (_span_of(attrs, "colspan"), _span_of(attrs, "rowspan"))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            colspan, rowspan = self._span
            self._row.append(
                _TableCell(_clean_cell("".join(self._cell)), colspan=colspan, rowspan=rowspan)
            )
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell.text for cell in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _clean_cell(text: str) -> str:
    # 竖线必须转义：单元格里出现 `|` 会把 pipe 表的列切错，
    # 表头对不上后面的每一行，整张表都读歪。
    return " ".join(text.split()).replace("|", "\\|")


def _expand(rows: list[list[_TableCell]]) -> list[list[str]]:
    """把合并单元格摊平成等宽网格。

    不摊平会直接错位：国标表头几乎都是 `colspan="2"` / `rowspan="2"`，
    而数据行是一个单元格一列——表头 3 格、数据 8 格时，后面的列整体前移，
    引用里看起来像「适用范围写反了」。这不是排版问题，是事实会被读错。
    """
    occupied: dict[tuple[int, int], str] = {}
    grid: list[list[str]] = []

    for r, row in enumerate(rows):
        line: list[str] = []
        col = 0

        for cell in row:
            while (r, col) in occupied:
                line.append(occupied[(r, col)])
                col += 1

            line.append(cell.text)

            # 横向合并：内容只在第一格，其余留空。
            line.extend("" for _ in range(cell.colspan - 1))
            # 纵向合并：接下来几行的这几列由它占着，内容不重复写。
            for dr in range(1, cell.rowspan):
                for dc in range(cell.colspan):
                    occupied[(r + dr, col + dc)] = ""

            col += cell.colspan

        # 上一行 rowspan 延伸下来、又落在本行末尾的列。
        while (r, col) in occupied:
            line.append(occupied[(r, col)])
            col += 1

        grid.append(line)

    width = max((len(line) for line in grid), default=0)

    return [line + [""] * (width - len(line)) for line in grid]


def table_to_markdown(table_html: str) -> str:
    """HTML 表格 → Markdown 表格；解析不出行就退化成空串（宁缺勿噪）。"""
    parser = _TableParser()
    parser.feed(table_html)
    parser.close()

    grid = _expand([row for row in parser.rows if row])

    if not grid:
        return ""

    header, *body = grid

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]

    lines.extend("| " + " | ".join(row) + " |" for row in body)

    return "\n".join(lines)


def looks_taggy(text: str) -> bool:
    """这份文本是不是带着 OCR 的 HTML 标签。入库路径用它决定要不要清洗。"""
    lowered = text.lower()
    return any(marker in lowered for marker in _TAGGY_MARKERS)


def normalize(text: str, *, boilerplate: set[str] | None = None) -> str:
    """去表格标签味、去图、去包裹层标签，压掉多余空行。"""
    skip = boilerplate or set()

    text = _TABLE_RE.sub(lambda match: "\n\n" + table_to_markdown(match.group(0)) + "\n\n", text)
    text = _IMG_RE.sub("", text)
    # div / span 之类的包裹层只去标签、留文字：题注就在里面。
    text = _TAG_RE.sub("", text)

    lines: list[str] = []

    for raw in text.splitlines():
        line = " ".join(raw.split())

        if not line:
            lines.append("")
            continue
        if line in skip:
            continue

        lines.append(line)

    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def clean_ocr_markdown(text: str) -> tuple[str, dict[str, int]]:
    """入库前的形态清洗。返回 (正文, 统计)。

    没有标签味就原样返回、统计为空——**不白跑一遍**，也不会让「本来干净的 md」
    因为经过这道函数而丢掉空行之类的细节。
    """
    if not looks_taggy(text):
        return text, {}

    tables = len(_TABLE_RE.findall(text))
    cleaned = normalize(text)

    if not cleaned:
        # 全篇只有标签、没有文字：不能返回空串让上层以为「文档没有正文」，
        # 那会让「这份 OCR 产物是坏的」变成「你传了个空文件」。
        return text, {"tables": tables, "stripped": 0, "kept_original": 1}

    return cleaned, {
        "tables": tables,
        "chars_before": len(text),
        "chars_after": len(cleaned),
    }
