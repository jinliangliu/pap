# app/knowledge/ingest.py
"""文档入库：类型识别 → 安全排除 → 文本抽取 → 元数据 → 切片 → 写索引。

安全默认：密钥、凭据、数据库备份、日志、构建产物与二进制文件一律不入库。
每份文档保留 content_hash、access_scope、updated_at，支持按原始资料重建索引。

两条入口共用同一套解析：

- 目录扫描（`parse_path`）：运维放进 `KNOWLEDGE_DIR` 的文件，视为该租户的全集；
- 页面上传（`extract_text_from_bytes`）：先解析、后落盘，解析失败不写磁盘。

抽取与切片都在这里完成，写入交给 `documents` 模块——那份文件管增量与生命周期，
本模块只管「把一份资料变成切片」。
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.core.config import settings

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".html", ".htm"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES

# 默认排除：目录名与文件名模式。
EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "logs",
    "dist",
    "build",
}

EXCLUDED_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "token.json",
    "cookies.txt",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.sql.gz",
    "*.log",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.mp4",
    "*.exe",
    "*.dll",
    "*.so",
    "*.pyc",
)

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# 切片规则每次改动都要 +1。增量判定的「内容哈希相同 + 切片数相同」看不出
# 「切法变了」：一篇只有一个标题、切出来仍是 1 片的文档，section 从
# 「第 1 段」变成「技术原则」，两条判据都不变，会被误判为 unchanged 永远不重建。
# 这个版本号写进 documents.metadata_json，跟着一起比。
CHUNKER_VERSION = 2

# Markdown h1–h3，以及中文编号标题（「一、启动前检查」）。
# 不认「1. xxx」：那是正文里的有序列表，认了会把操作手册切成一堆碎片。
_HEADING_MD = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_HEADING_CN = re.compile(r"^([一二三四五六七八九十]+)、\s*(.+)$")
SECTION_JOINER = " / "
NO_HEADING_LABEL = "正文"

# access_scope → 需要的 scope。高敏感数据默认禁止入库。
SCOPE_REQUIREMENTS = {
    "public": "knowledge.public",
    "internal": "knowledge.internal",
    "confidential": "knowledge.confidential",
}

VALID_SCOPES = {"public", "internal", "confidential"}
VALID_VISIBILITY = {"tenant_shared", "user_private"}

# 目录扫描的文档来源标记；上传用 "upload"，两者在同步时互不干扰。
SOURCE_DIRECTORY = "file"
SOURCE_UPLOAD = "upload"


class KnowledgeIngestError(RuntimeError):
    pass


@dataclass
class IngestReport:
    documents: int = 0
    chunks: int = 0
    skipped: list[str] = field(default_factory=list)
    # 本次写入的目标（数据库表 + 租户），不再是本地文件路径。
    target: str = ""
    # 需要额外留一份 JSON 快照时才填。
    export_path: str = ""
    # 增量同步的结果：内容没变的文档不必重建，目录里消失的文档要下架。
    unchanged: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class ParsedDocument:
    """一份待写入的文档：元数据 + 切好的片段。

    `logical_id` 是跨次入库稳定的逻辑标识（目录扫描用相对路径派生，
    上传用首次上传时生成的 id），`documents.id` 由它与租户共同派生。
    """

    logical_id: str
    title: str
    filename: str | None
    source_uri: str
    source_type: str
    content_hash: str
    updated_at: str
    size_bytes: int | None
    acl: dict[str, Any]
    chunks: list[str]
    # 与 chunks 一一对应的小节标题。检索引用展示的就是它，
    # 「第 1 段」这种标签对用户没有任何定位价值。
    sections: list[str]
    chunker_version: int = CHUNKER_VERSION

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def is_excluded(path: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return True

    name = path.name.lower()

    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in EXCLUDED_FILE_PATTERNS)


def split_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """按自然段优先切分，保留少量重叠上下文。"""
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n{paragraph}".strip()

        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(paragraph) > chunk_size:
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + chunk_size].strip())
                start += chunk_size - CHUNK_OVERLAP
            current = ""
        else:
            overlap = current[-CHUNK_OVERLAP:] if current else ""
            current = f"{overlap}\n{paragraph}".strip()

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk]


@dataclass(frozen=True)
class Section:
    text: str
    section: str


def _heading_of(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    match = _HEADING_MD.match(stripped)

    if match:
        return len(match.group(1)), match.group(2).strip()

    match = _HEADING_CN.match(stripped)

    if match:
        # 中文编号标题按二级处理：它上面通常还有一行文档大标题。
        return 2, match.group(2).strip()

    return None


def split_document(text: str, chunk_size: int = CHUNK_SIZE) -> list[Section]:
    """按标题分节，再在节内按自然段切片。

    标题行**留在正文里**：BM25 命中标题词本来就是强相关信号，丢掉反而变差。
    没有标题的文档（PDF / Word / HTML 抽出来的纯文本、无标题 txt）回落到
    原来的自然段切片，section 仍是「第 N 段」，不破坏旧数据的展示习惯。
    """
    blocks: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current_label = ""
    current_lines: list[str] = []
    in_fence = False
    seen_heading = False

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        heading = None if in_fence else _heading_of(line)

        if heading is None:
            current_lines.append(line)
            continue

        level, title = heading

        while stack and stack[-1][0] >= level:
            stack.pop()

        stack.append((level, title))

        if any(item.strip() for item in current_lines):
            blocks.append((current_label, current_lines))

        # 只保留最近两级，避免长文档里出现「A / B / C / D / E」这种没法读的标签。
        current_label = SECTION_JOINER.join(label for _, label in stack[-2:])
        current_lines = [line]
        seen_heading = True

    if any(item.strip() for item in current_lines):
        blocks.append((current_label, current_lines))

    if not seen_heading:
        return [
            Section(text=piece, section=f"第 {index + 1} 段")
            for index, piece in enumerate(split_text(text, chunk_size))
        ]

    result: list[Section] = []

    for label, lines in blocks:
        body = "\n".join(lines).strip()

        if not body:
            continue

        pieces = split_text(body, chunk_size)
        base = label or NO_HEADING_LABEL

        for index, piece in enumerate(pieces):
            name = base if len(pieces) == 1 else f"{base}（{index + 1}/{len(pieces)}）"
            result.append(Section(text=piece, section=name[:200]))

    return result


def _extract_pdf(source: str | Path | BinaryIO) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 声明
        raise KnowledgeIngestError("未安装 pypdf，无法解析 PDF。") from exc

    reader = PdfReader(str(source) if isinstance(source, Path) else source)

    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(source: str | Path | BinaryIO) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 声明
        raise KnowledgeIngestError("未安装 python-docx，无法解析 Word。") from exc

    document = docx.Document(str(source) if isinstance(source, Path) else source)

    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _html_to_text(markup: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 依赖已在 pyproject 声明
        return markup

    soup = BeautifulSoup(markup, "html.parser")

    return soup.get_text("\n")


def extract_text_from_bytes(raw: bytes, filename: str) -> str:
    """从字节解析文本。

    上传先解析、后落盘：解析失败就不该在磁盘上留下一个永远不会被索引的
    孤儿文件；而落盘后再解析，失败时清理不干净就会一直占空间。
    """
    suffix = Path(filename).suffix.lower()

    if suffix in TEXT_SUFFIXES:
        text = raw.decode("utf-8", errors="ignore")
    elif suffix == ".pdf":
        text = _extract_pdf(io.BytesIO(raw))
    elif suffix == ".docx":
        text = _extract_docx(io.BytesIO(raw))
    elif suffix in {".html", ".htm"}:
        text = _html_to_text(raw.decode("utf-8", errors="ignore"))
    else:
        raise KnowledgeIngestError(f"不支持的文件类型：{suffix or '(无扩展名)'}")

    # 行尾归一。同一份文档在 Windows 上写成 CRLF、经管道/内存再来一遍是 LF，
    # 正文看着一样但字节不同，于是内容哈希不同、「重复上传不重复入库」失效。
    # 实测：把 27791 的 .md 和它的内存转换版各传一次，库里多出一份 48 片的副本。
    # \r 留在切片里还会污染摘录展示与 BM25 的换行边界。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_text(path: Path) -> str:
    return extract_text_from_bytes(path.read_bytes(), path.name)


def logical_id_for_path(relative_path: str) -> str:
    """目录扫描文档的稳定标识。

    由相对路径派生而不是内容哈希：文件被编辑后仍指向同一份文档，
    引用不会因为改了一个字就全部失效。
    """
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    stem = Path(relative_path).stem.replace(" ", "-")

    return f"{stem}-{digest}"


# 兼容旧名：早期版本只有目录扫描，函数名直接叫 _document_id。
_document_id = logical_id_for_path


def _access_scope(relative_path: str) -> str:
    lowered = relative_path.lower()

    if "confidential" in lowered or "机密" in lowered:
        return "confidential"
    if "public" in lowered or "公开" in lowered:
        return "public"

    return "internal"


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """解析可选的 `---` 头信息。

    只支持 `key: value` 与逗号分隔的列表，不引入 YAML 依赖：
    元数据格式越简单，越不容易把「解析失败」变成「权限配置被忽略」。
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        return {}, text

    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}, text

    meta: dict[str, str] = {}

    for line in lines[1:end]:
        if not line.strip() or line.strip().startswith("#"):
            continue

        key, separator, value = line.partition(":")

        if not separator:
            continue

        meta[key.strip().lower()] = value.strip()

    return meta, "\n".join(lines[end + 1 :])


def _document_acl(relative_path: str, meta: dict[str, str]) -> dict:
    """
    文档级 ACL。

    优先级：头信息 > 路径推断。
    非法值一律回落到最保守的可用值，user_private 缺少 owner 时直接拒绝入库
    （宁可少入库一份，也不能把「本该私有」的文档变成全员可见）。
    """
    scope = (meta.get("access_scope") or _access_scope(relative_path)).strip().lower()

    if scope not in VALID_SCOPES:
        scope = _access_scope(relative_path)

    visibility = (meta.get("visibility") or "tenant_shared").strip().lower()

    if visibility not in VALID_VISIBILITY:
        visibility = "tenant_shared"

    allowed_roles = [
        role.strip() for role in (meta.get("allowed_roles") or "").split(",") if role.strip()
    ]
    owner_user_id = (meta.get("owner_user_id") or "").strip() or None

    if visibility == "user_private" and not owner_user_id:
        raise KnowledgeIngestError("user_private 文档必须声明 owner_user_id。")

    return {
        "access_scope": scope,
        "visibility": visibility,
        "allowed_roles": allowed_roles,
        "owner_user_id": owner_user_id,
    }


def build_document(
    text: str,
    *,
    logical_id: str,
    title: str,
    source_uri: str,
    source_type: str,
    filename: str | None = None,
    updated_at: str | None = None,
    size_bytes: int | None = None,
    meta: dict[str, str] | None = None,
    acl_hint: str | None = None,
) -> ParsedDocument:
    """把已抽取的正文变成可写入的文档。

    切片上限在这里拦：一份几百页的手册切成上万片，BM25 每轮检索都要扫一遍，
    与其让它慢慢拖垮检索，不如明确报错让调用方拆分文档。
    """
    body = text.strip()

    if not body:
        raise KnowledgeIngestError("文档没有可索引的正文。")

    sections = split_document(body)
    chunks = [item.text for item in sections]
    limit = settings.knowledge_max_chunks_per_document

    if len(chunks) > limit:
        raise KnowledgeIngestError(f"切片数 {len(chunks)} 超过单篇上限 {limit}，请拆分后再入库。")

    return ParsedDocument(
        logical_id=logical_id,
        title=(title or Path(filename or source_uri).stem or logical_id)[:512],
        filename=filename,
        source_uri=source_uri,
        source_type=source_type,
        content_hash="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        updated_at=updated_at or datetime.now(UTC).isoformat(),
        size_bytes=size_bytes,
        acl=_document_acl(acl_hint or source_uri, meta or {}),
        chunks=chunks,
        sections=[item.section for item in sections],
        chunker_version=CHUNKER_VERSION,
    )


def parse_path(path: Path, root: Path, *, source_type: str = SOURCE_DIRECTORY) -> ParsedDocument:
    """解析目录里的一个文件；调用方负责先做排除与大小判断。"""
    relative = path.relative_to(root).as_posix()
    raw = path.read_bytes()
    text = extract_text_from_bytes(raw, path.name)

    meta: dict[str, str] = {}

    if path.suffix.lower() in TEXT_SUFFIXES:
        meta, body = parse_front_matter(text)
        text = body

    # 二进制/HTML 抽出的文本不参与头信息解析，避免误删正文。
    return build_document(
        text,
        logical_id=logical_id_for_path(relative),
        title=meta.get("title") or path.stem,
        source_uri=relative,
        source_type=source_type,
        filename=path.name,
        updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        size_bytes=len(raw),
        meta=meta,
        acl_hint=relative,
    )


def _records_of(document: ParsedDocument) -> list[dict]:
    """把 ParsedDocument 摊成检索层认识的记录形状。

    检索层（BM25）按记录构造，写入层按文档构造；两者形状一致，
    换存储时只有这一处需要跟着改。
    """
    updated_at = document.updated_at

    return [
        {
            "chunk_id": f"{document.logical_id}-{index:04d}",
            "document_id": document.logical_id,
            "chunk_index": index,
            "text": chunk,
            "title": document.title,
            "source_path": document.source_uri,
            "document_type": "knowledge_document",
            "section": document.sections[index]
            if index < len(document.sections)
            else f"第 {index + 1} 段",
            "tags": [],
            "updated_at": updated_at,
            "content_hash": document.content_hash,
            **document.acl,
        }
        for index, chunk in enumerate(document.chunks)
    ]


def build_index(knowledge_dir: str | None = None) -> tuple[list[dict], IngestReport]:
    """扫描目录并返回检索记录（不写库）。

    保留这个纯函数是为了 `--export-json` 快照与单元测试：
    它不碰数据库，能单独验证「哪些文件会被入库、切成几片」。
    """
    root = Path(knowledge_dir or settings.knowledge_dir)

    if not root.exists():
        raise KnowledgeIngestError(f"知识目录不存在：{root}")

    report = IngestReport()
    records: list[dict] = []
    documents = scan_directory(root, report)

    for document in documents:
        report.documents += 1
        report.chunks += document.chunk_count
        records.extend(_records_of(document))

    return records, report


def scan_directory(root: Path, report: IngestReport | None = None) -> list[ParsedDocument]:
    """扫描目录，返回可入库的文档；被跳过的原因写进 report。"""
    report = report if report is not None else IngestReport()
    documents: list[ParsedDocument] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(root).as_posix()

        if is_excluded(path.relative_to(root)):
            report.skipped.append(f"{relative}:excluded")
            continue

        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            report.skipped.append(f"{relative}:unsupported")
            continue

        if path.stat().st_size > settings.knowledge_max_file_bytes:
            report.skipped.append(f"{relative}:too_large")
            continue

        try:
            documents.append(parse_path(path, root))
        except KnowledgeIngestError as exc:
            report.skipped.append(f"{relative}:{exc}")
        except Exception as exc:  # 解析异常不能中断整批入库
            logger.warning("文档解析失败：%s (%s)", relative, exc)
            report.skipped.append(f"{relative}:parse_error")

    return documents


def write_index(records: list[dict], output: str | None = None) -> str:
    """导出 JSON 快照（排查、备份用），不是运行时索引。"""
    target = Path(output or settings.knowledge_store_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return str(target)


async def rebuild(
    knowledge_dir: str | None = None,
    *,
    tenant_id: str | None = None,
    export_json: bool = False,
) -> IngestReport:
    """
    把知识目录同步进某个租户（增量）。

    早期这里是「全量替换」：先删光该租户所有文档再插。上传功能上来之后
    这个语义会出事——一次目录同步就把页面上传的文档全删了。现在只同步
    `source_type='file'` 的文档，上传的文档由自己的接口管。
    """
    from app.db.session import async_session_factory
    from app.knowledge.documents import sync_directory

    target_tenant = tenant_id or settings.default_tenant_id
    root = Path(knowledge_dir or settings.knowledge_dir)
    report = IngestReport()

    documents = scan_directory(root, report)

    async with async_session_factory() as session:
        result = await sync_directory(session, target_tenant, documents)
        await session.commit()

    report.documents = result.documents
    report.chunks = result.chunks
    report.unchanged = result.unchanged
    report.deleted = result.deleted
    report.target = f"documents/document_chunks@tenant={target_tenant}"

    if export_json:
        records: list[dict] = []
        for document in documents:
            records.extend(_records_of(document))
        report.export_path = write_index(records)

    return report
