# scripts/ocr_json_to_md.py
"""PaddleOCR-VL 的结果 JSON → 可直接入库的 Markdown。

## 为什么需要这一步

PaddleOCR-VL 产物是「每页一个 markdown + 一堆版面块」的 JSON，而知识库只收
`.md` / `.txt` / `.pdf` / `.docx` / `.html`。直接把 JSON 当 `.md` 传上去，
切片里会全是 `{"prunedResult": ...}` 这种噪声，BM25 召回的也全是键名。

## 转换时做的四件事（都不是格式化，都会影响检索）

1. **丢掉页眉页脚水印**。扫描件的每页都带「标准资料网 WWW.PV265.COM」和
   标准号，重复上百次之后这两个串会变成全库最高频的词，任何查询都能命中它们。
2. **HTML 表格转成 Markdown 表格**。`<td style='text-align: center; word-wrap:
   break-word;'>` 里的属性名会被切成 token，把 `word`/`break`/`center` 喂进索引。
3. **图片与 div 包裹层丢掉，但留下题注文字**。图片本身进不了索引，
   而「表1 调压管道结构代号」这种题注是定位正文的关键。
4. **保留标题层级**（`##` / `###`）。切片按标题分节，`section` 就是引用里
   展示的那一栏；抹平层级会退化成「第 N 段」。

用法::

    python -m scripts.ocr_json_to_md input.json                  # 输出同名 .md
    python -m scripts.ocr_json_to_md input.json -o out/标准.md
    python -m scripts.ocr_json_to_md input.json --stats          # 只看统计不落盘
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# PaddleOCR 标成这些标签的块不进正文：页码、脚注、页眉页脚、侧栏。
# 与 JSON 里 `markdown_ignore_labels` 的默认值保持一致。
IGNORED_LABELS = {
    "number",
    "footnote",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "aside_text",
}

_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_IMG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BLANK_RE = re.compile(r"\n{3,}")


class _TableParser(HTMLParser):
    """把 `<table>` 采成二维字符串数组。

    只用标准库：这个脚本要能在没装 bs4 的机器上跑（服务器上是空的 venv）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_clean_cell("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _clean_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def table_to_markdown(table_html: str) -> str:
    """HTML 表格 → Markdown 表格；解析不出行就退化成空串（宁缺勿噪）。"""
    parser = _TableParser()
    parser.feed(table_html)
    parser.close()

    rows = [row for row in parser.rows if row]

    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded

    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in range(width)) + " |"]

    lines.extend("| " + " | ".join(row) + " |" for row in body)

    return "\n".join(lines)


def boilerplate_lines(pages: list[dict[str, Any]], *, min_pages: int = 2) -> set[str]:
    """找出在多页重复出现的页眉页脚原文。

    按「出现过至少 ``min_pages`` 页」判，而不是写死标准号与水印串——
    换一份扫描件，水印就换了，规则得跟着数据走。
    """
    seen: dict[str, set[int]] = {}

    for index, page in enumerate(pages):
        for block in _blocks(page):
            if block.get("block_label") not in {"header", "footer", "aside_text"}:
                continue
            text = " ".join(str(block.get("block_content") or "").split())
            if text:
                seen.setdefault(text, set()).add(index)

    return {text for text, pages_seen in seen.items() if len(pages_seen) >= min_pages}


def _blocks(page: dict[str, Any]) -> list[dict[str, Any]]:
    pruned = page.get("prunedResult") or {}
    blocks = pruned.get("parsing_res_list") or []
    return [block for block in blocks if isinstance(block, dict)]


def page_text(page: dict[str, Any]) -> str:
    """取一页的正文：优先用 markdown，缺了就从版面块按顺序拼。"""
    markdown = page.get("markdown")

    if isinstance(markdown, dict) and isinstance(markdown.get("text"), str):
        return markdown["text"]

    ordered = sorted(
        (block for block in _blocks(page)
         if block.get("block_label") not in IGNORED_LABELS),
        key=lambda block: block.get("block_order") or 0,
    )

    return "\n\n".join(str(block.get("block_content") or "") for block in ordered)


def normalize(text: str, *, boilerplate: set[str]) -> str:
    """去表格标签味、去图、去重复页眉页脚，压掉多余空行。"""
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
        if line in boilerplate:
            continue

        lines.append(line)

    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def convert(data: Any) -> tuple[str, dict[str, Any]]:
    """整份 JSON → (markdown, 统计)。"""
    pages = data if isinstance(data, list) else [data]
    pages = [page for page in pages if isinstance(page, dict)]

    if not pages:
        raise ValueError("JSON 里没有页面数据，确认这是 PaddleOCR-VL 的结果文件。")

    boilerplate = boilerplate_lines(pages)
    chunks: list[str] = []
    seen: set[str] = set()
    duplicates = 0

    for page in pages:
        body = normalize(page_text(page), boilerplate=boilerplate)

        if not body:
            continue

        # 扫描件里重复页是常见产物（GB 27791 的第 5 页就被 OCR 了两次）。
        # 不去重的话同一段正文会入库两遍，检索时占掉两个引用位。
        if body in seen:
            duplicates += 1
            continue

        seen.add(body)
        chunks.append(body)

    if not chunks:
        # 空正文不能当成功：传上去的是一份没人能检索到的空文档。
        raise ValueError("这份 JSON 里没有可提取的正文（不是 PaddleOCR-VL 的结果？）")

    markdown = "\n\n".join(chunks)
    markdown = _BLANK_RE.sub("\n\n", markdown).strip() + "\n"

    stats = {
        "pages": len(pages),
        "pages_with_text": len(chunks),
        "boilerplate": sorted(boilerplate),
        "chars": len(markdown),
        "headings": len(re.findall(r"^#{1,6} ", markdown, flags=re.MULTILINE)),
        "tables": markdown.count("\n| ---"),
        "duplicate_pages": duplicates,
    }

    return markdown, stats


def output_path_for(source: Path) -> Path:
    """`xxx.pdf_by_PaddleOCR-VL-1.6.json` → 同名 `.md`（与 docs/ 下既有命名一致）。"""
    return source.with_suffix(".md")


def normalize_markdown(text: str) -> str:
    """就地规整一份已经是 Markdown 的 OCR 产物。

    PaddleOCR-VL 有时直接吐出 `.md`（而不是 JSON），里面的表格仍然是
    `<table border=1 style=...><td>` 那一坨。表格是标准里信息密度最高的部分，
    原样入库会让每一条引用都拖着标签，读起来全是噪声，所以这里复用 JSON 路径
    的清洗逻辑：表格转 pipe 表、去掉包裹层标签、压掉多余空行。
    """
    return normalize(text, boilerplate=set())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocr_json_to_md",
        description="把 PaddleOCR-VL 的结果 JSON 转成可入库的 Markdown",
    )
    parser.add_argument("source", help="OCR 结果 JSON（或 --from-md 时的 .md）路径")
    parser.add_argument("-o", "--output", help="输出路径，默认与输入同名 .md")
    parser.add_argument(
        "--from-md",
        action="store_true",
        help="输入已经是 Markdown（只做表格/标签清洗），默认输出到 *.norm.md",
    )
    parser.add_argument("--stats", action="store_true", help="只打印统计，不写文件")
    parser.add_argument("--json", action="store_true", help="统计以 JSON 输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.source)

    if not source.exists():
        print(f"错误：文件不存在 {source}", file=sys.stderr)
        return 2

    if args.from_md:
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"错误：读不了这份文件（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return 2

        markdown = normalize_markdown(raw)
        stats = {
            "mode": "md",
            "pages": 1,
            "pages_with_text": 1,
            "duplicate_pages": 0,
            "boilerplate": [],
            "chars": len(markdown),
            "headings": len(re.findall(r"^#{1,6} ", markdown, flags=re.MULTILINE)),
            "tables": markdown.count("\n| ---"),
        }
    else:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"错误：读不了这份 JSON（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return 2

        try:
            markdown, stats = convert(data)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    if not args.stats:
        if args.output:
            target = Path(args.output)
        elif args.from_md:
            target = source.with_name(source.stem + ".norm.md")
        else:
            target = output_path_for(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        stats["output"] = str(target)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        if args.from_md:
            print(f"字数      {stats['chars']}")
            print(f"标题      {stats['headings']} 个 · 表格 {stats['tables']} 个")
        else:
            print(f"页数      {stats['pages']}（有正文 {stats['pages_with_text']}，"
                  f"重复页剔除 {stats['duplicate_pages']}）")
            print(f"字数      {stats['chars']}")
            print(f"标题      {stats['headings']} 个 · 表格 {stats['tables']} 个")
            print(f"页眉页脚  {len(stats['boilerplate'])} 种，已剔除")
            for line in stats["boilerplate"][:5]:
                print(f"          - {line}")
        if stats.get("output"):
            print(f"输出      {stats['output']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
