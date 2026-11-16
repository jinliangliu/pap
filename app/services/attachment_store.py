# app/services/attachment_store.py
"""对话附件的落盘与回收（图片、PDF 与文本类文件）。

为什么不把文件原样留在 LangGraph state 里：
chat 请求里的 data_url 是 base64 内联，动辄数百 KB 到数 MB。
写进 checkpoint 会让整条会话的每次恢复都反序列化这份数据，
既拖慢恢复也放大数据库体积。因此这里落盘成文件，
state 里只保留 attachment_id 这类不可推断的句柄。

安全约束：
- attachment_id 由服务端生成，格式固定，不接受外部构造的路径；
- 解析路径必须落在附件根目录内（防目录穿越）；
- 扩展名来自服务端根据 MIME 决定的白名单，不取客户端文件名；
- 声明类型与真实内容不一致时**以内容为准**，扩展名不参与判断。

PDF 支持是后加的，因此这里统一叫「附件」而不是「图片」：
`img_` 前缀留给图片，`doc_` 前缀留给 PDF，`txt_` 留给文本类文件（Markdown / 日志 / 配置），
三者共用同一套解析逻辑。旧会话里已经落盘的 `img_` / `doc_` 文件不受影响。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

# 句柄形如 img_9f2c1a4b7e60 / doc_9f2c1a4b7e60 / txt_9f2c1a4b7e60
ATTACHMENT_ID_RE = re.compile(r"^(img|doc|txt)_[0-9a-f]{12}$")

PDF_MIME = "application/pdf"
MARKDOWN_MIME = "text/markdown"
TEXT_MIME = "text/plain"

# 文本类附件的全部类型。读取工具据此决定「直接读盘」还是「送 OCR」。
TEXT_MIMES = frozenset({MARKDOWN_MIME, TEXT_MIME})

# 文本附件的扩展名白名单。
# 不能只信客户端声明的 MIME：Windows 上的浏览器给 `.md` 的 `File.type`
# 常常是空串，data URL 会变成 `data:application/octet-stream;base64,...`，
# 所以后缀是这里唯一稳定的线索（对照表见 docs/CONVERSATIONS.md §3b）。
#
# 收的不只是 Markdown：构建助手的文本附件实际有三种用途——
# 建知识库（.md）、排查问题（.log / .jsonl 这类运行日志）、
# 新增配置（.yaml / .json / .toml / .ini）。按用途挑后缀等于让用户
# 先猜「我的文件算哪一类」，而这三类都是纯文本，读取链路完全一样。
# 判别仍然只有一道：后缀在白名单里 + 内容形态像文本（`_normalize_text`）。
_TEXT_SUFFIXES: dict[str, str] = {
    ".md": MARKDOWN_MIME,
    ".markdown": MARKDOWN_MIME,
    ".txt": TEXT_MIME,
    ".text": TEXT_MIME,
    # 文档：README、说明、规范
    ".rst": TEXT_MIME,
    # 运行日志 / 导出的追踪记录
    ".log": TEXT_MIME,
    ".jsonl": TEXT_MIME,
    ".ndjson": TEXT_MIME,
    # 结构化数据与配置
    ".json": TEXT_MIME,
    ".yaml": TEXT_MIME,
    ".yml": TEXT_MIME,
    ".toml": TEXT_MIME,
    ".ini": TEXT_MIME,
    ".cfg": TEXT_MIME,
    ".conf": TEXT_MIME,
    ".env": TEXT_MIME,
    ".csv": TEXT_MIME,
    ".tsv": TEXT_MIME,
    ".xml": TEXT_MIME,
}

# 客户端声明的文本类 MIME → 归一化后的类型。
# 浏览器会老老实实给 `.json` 报 `application/json`、`.csv` 报 `text/csv`，
# 这些不是非法类型，只是与上面两个规范 MIME 不同——一律收敛到
# `text/markdown` / `text/plain`，读取与展示就只需要认这两档。
_DECLARED_TEXT_MIMES: dict[str, str] = {
    MARKDOWN_MIME: MARKDOWN_MIME,
    TEXT_MIME: TEXT_MIME,
    "text/x-markdown": MARKDOWN_MIME,
    "application/json": TEXT_MIME,
    "application/x-ndjson": TEXT_MIME,
    "application/x-yaml": TEXT_MIME,
    "application/yaml": TEXT_MIME,
    "text/yaml": TEXT_MIME,
    "application/toml": TEXT_MIME,
    "application/xml": TEXT_MIME,
    "text/xml": TEXT_MIME,
    "text/csv": TEXT_MIME,
    "text/tab-separated-values": TEXT_MIME,
}

# 「客户端没给类型」的几种声明，允许继续按内容与后缀判断。
_UNKNOWN_DECLARED = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# 文本附件上限。真正的上下文闸在读取时的 `ocr_max_text_chars`（4 万字符），
# 这里的 4 MB 只用来挡住「拿一个几 GB 的日志冒充附件」。
_TEXT_MAX_BYTES = 4 * 1024 * 1024

# MIME → 扩展名白名单。不在表内的一律拒绝，不做猜测。
_ALLOWED_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    PDF_MIME: ".pdf",
    MARKDOWN_MIME: ".md",
    TEXT_MIME: ".txt",
}

# 读取文件时按内容嗅探出的类型也必须在这个集合里。
_ALLOWED_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"BM", "image/bmp"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", PDF_MIME),
)

# OCR 服务的入参是多文件表单，文件名只用来判断扩展名，不代表原始文件名。
_SUFFIX_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    PDF_MIME: ".pdf",
    MARKDOWN_MIME: ".md",
    TEXT_MIME: ".txt",
}


class AttachmentError(RuntimeError):
    """附件不可用（格式非法、超限、越权或已过期）。"""


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    attachment_id: str
    kind: str  # image | pdf
    mime: str
    size: int
    name: str | None = None


def kind_of(mime: str) -> str:
    if mime == PDF_MIME:
        return "pdf"
    if mime in TEXT_MIMES:
        return "text"
    return "image"


def suffix_for(mime: str) -> str:
    """OCR 服务按文件名扩展名分发，这里给出可信的扩展名。"""
    return _SUFFIX_BY_MIME.get(mime, ".bin")


def _safe_slug(value: str) -> str:
    """把租户/会话标识收敛成安全目录名，避免路径分隔符与 ':' 之类字符。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "")
    return cleaned[:64] or "unknown"


def _root() -> Path:
    return Path(settings.ocr_attachment_dir).expanduser().resolve()


def _target_dir(*, tenant_id: str, conversation_id: str) -> Path:
    return _root() / _safe_slug(tenant_id) / _safe_slug(conversation_id)


def sniff_mime(data: bytes) -> str | None:
    """按文件头判断类型；识别不出返回 None。"""
    for magic, mime in _ALLOWED_MAGIC:
        if data.startswith(magic):
            return mime

    # WebP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    return None


def _limit_for(mime: str, *, max_image_bytes: int, max_pdf_bytes: int) -> int:
    if mime == PDF_MIME:
        return max_pdf_bytes
    if mime in TEXT_MIMES:
        return _TEXT_MAX_BYTES
    return max_image_bytes


def max_bytes_for(
    mime: str, *, max_image_bytes: int, max_pdf_bytes: int
) -> int:
    """某类附件允许的原始字节上限。

    公开出来是为了让读取工具与产物转交（`reference_service`）用同一套判据：
    各自抄一份 `if mime == PDF ... else ...`，加第三种类型时必然漏改一处。
    """
    return _limit_for(mime, max_image_bytes=max_image_bytes, max_pdf_bytes=max_pdf_bytes)


def _looks_binary(data: bytes) -> bool:
    """粗判二进制：出现 NUL，或控制字符占比超过 1%。

    「能用 GB18030 解码」不能当作「这是文本」的证据——GB18030 几乎覆盖
    所有字节序列，一张 JPEG 也能「解码」成一串乱码。所以先看形态，再解码。
    """
    head = data[:8192]
    if b"\x00" in head:
        return True
    # \t \n \v \f \r 是文本里正常的空白，不算控制字符。
    control = sum(1 for byte in head if byte < 0x09 or 0x0E <= byte < 0x20)
    return control > max(1, len(head) // 100)


def _normalize_text(data: bytes) -> bytes | None:
    """把文本统一成 UTF-8 字节；不像文本时返回 None。

    统一编码是必须的：读取工具按 UTF-8 打开，GBK 的 `.md` 落盘后读出来
    会是乱码，而且那种乱码看起来很像「文件内容本来就是这样」，没人会怀疑。
    """
    if not data or _looks_binary(data):
        return None

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text.encode("utf-8")
    return None


def _sniff_text(
    data: bytes, *, declared: str, name: str | None
) -> tuple[bytes, str] | None:
    """按后缀 / 声明类型判定是不是受支持的文本附件。

    后缀不认识、声明也不是文本类型时返回 None——不靠「看着像文本」收下
    来路不明的文件。这样 `.docx`、`.exe` 改个名字也进不来。
    """
    mime = _TEXT_SUFFIXES.get(Path(name or "").suffix.lower())
    if mime is None:
        mime = _DECLARED_TEXT_MIMES.get(declared)
    if mime is None:
        return None

    normalized = _normalize_text(data)
    if normalized is None:
        return None
    return normalized, mime


def decode_data_url(
    data_url: str,
    *,
    max_image_bytes: int,
    max_pdf_bytes: int | None = None,
    name: str | None = None,
) -> tuple[bytes, str]:
    """解析 `data:image/png;base64,....` 这类 data URL，返回 (字节, MIME)。

    先按「各类里较大的上限」做一次编码长度预检，
    避免把几百 MB 的 base64 全解出来才发现超限。

    `name` 参与类型判定：文本附件没有文件头，只能靠后缀。
    文本会被统一转成 UTF-8 字节再返回，落盘后即可按 UTF-8 直接读。
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise AttachmentError("附件数据格式不正确。")

    header, _, payload = data_url.partition(",")
    if not payload:
        raise AttachmentError("附件数据为空。")

    if ";base64" not in header:
        raise AttachmentError("附件必须使用 base64 内联。")

    pdf_limit = max_pdf_bytes if max_pdf_bytes is not None else max_image_bytes
    hard_limit = max(max_image_bytes, pdf_limit, _TEXT_MAX_BYTES)

    # base64 每 4 字符还原 3 字节，留 4 字节余量给填充差异。
    if len(payload) // 4 * 3 > hard_limit + 4:
        raise AttachmentError(f"附件超过大小上限（{hard_limit} 字节）。")

    declared = header[5:].split(";")[0].strip().lower()
    # 浏览器对 `.md` / `.txt` 常常报 `application/octet-stream` 或空串，
    # 对 `.json` / `.csv` 则会报各自的规范 MIME。这几种都不当作非法类型，
    # 交给后面的内容与后缀判定；归一化在 `_DECLARED_TEXT_MIMES` 里。
    if (
        declared
        and declared not in _ALLOWED_MIME
        and declared not in _UNKNOWN_DECLARED
        and declared not in _DECLARED_TEXT_MIMES
    ):
        raise AttachmentError(f"不支持的附件类型：{declared}")

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("附件数据无法解码。") from exc

    if not raw:
        raise AttachmentError("附件数据为空。")

    sniffed = sniff_mime(raw)
    if sniffed is None:
        text = _sniff_text(raw, declared=declared, name=name)
        if text is None:
            raise AttachmentError(
                "无法识别的附件内容（支持图片、PDF、Markdown、纯文本、日志与配置文件）。"
            )
        raw, sniffed = text

    limit = _limit_for(sniffed, max_image_bytes=max_image_bytes, max_pdf_bytes=pdf_limit)
    if len(raw) > limit:
        label = {"pdf": "PDF", "text": "文本附件"}.get(kind_of(sniffed), "图片")
        raise AttachmentError(f"{label}超过大小上限（{limit // 1024 // 1024} MB）。")

    # 声明类型与真实内容不一致时以真实内容为准，避免用扩展名欺骗后续处理。
    return raw, sniffed


def save_bytes(
    *,
    tenant_id: str,
    conversation_id: str,
    raw: bytes,
    mime: str,
    name: str | None = None,
) -> StoredAttachment:
    """把已校验的字节落盘，返回句柄。

    与 `decode_data_url` 分开，是为了让调用方能在「建会话之前」完成校验：
    校验失败时不应该留下半个会话和一条孤立的 run 记录。
    """
    if mime not in _ALLOWED_MIME:
        raise AttachmentError(f"不支持的附件类型：{mime}")

    kind = kind_of(mime)
    prefix = {"pdf": "doc", "text": "txt"}.get(kind, "img")
    attachment_id = f"{prefix}_{secrets.token_hex(6)}"

    directory = _target_dir(tenant_id=tenant_id, conversation_id=conversation_id)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{attachment_id}{_ALLOWED_MIME[mime]}"
    path.write_bytes(raw)

    return StoredAttachment(
        attachment_id=attachment_id,
        kind=kind,
        mime=mime,
        size=len(raw),
        name=(name or "").strip()[:120] or None,
    )


def save_attachment(
    *,
    tenant_id: str,
    conversation_id: str,
    data_url: str,
    max_image_bytes: int,
    max_pdf_bytes: int | None = None,
    name: str | None = None,
) -> StoredAttachment:
    """把内联附件校验并落盘，返回句柄。"""
    raw, mime = decode_data_url(
        data_url,
        max_image_bytes=max_image_bytes,
        max_pdf_bytes=max_pdf_bytes,
        name=name,
    )
    return save_bytes(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        raw=raw,
        mime=mime,
        name=name,
    )


def resolve_attachment(
    *,
    tenant_id: str,
    conversation_id: str,
    attachment_id: str,
) -> tuple[Path, str]:
    """按句柄定位附件，返回 (路径, MIME)。

    只允许访问请求方自己的租户/会话目录，且必须命中已保存的文件。
    """
    if not isinstance(attachment_id, str) or not ATTACHMENT_ID_RE.match(attachment_id):
        raise AttachmentError("附件句柄格式不正确。")

    directory = _target_dir(tenant_id=tenant_id, conversation_id=conversation_id)

    for mime, suffix in _ALLOWED_MIME.items():
        candidate = directory / f"{attachment_id}{suffix}"
        if not candidate.is_file():
            continue

        resolved = candidate.resolve()

        # 纵深防御：即便目录名被意外污染，也不允许跳出附件根目录。
        if not resolved.is_relative_to(_root()):
            raise AttachmentError("附件路径越界。")

        return resolved, mime

    raise AttachmentError("附件不存在或已过期，请重新上传。")


def describe_for_prompt(
    *,
    attachment_id: str,
    kind: str,
    mime: str,
    size: int,
    name: str | None = None,
) -> str:
    """生成给模型看的附件说明行。

    模型只看得到句柄与元数据，看不到内容本身——
    这正是读取工具（`ocr_document`）存在的意义。
    """
    if kind == "pdf":
        label = "PDF"
    elif kind == "text":
        label = "Markdown" if mime == MARKDOWN_MIME else "文本"
    else:
        label = "图片"
    parts = [f"- file_id={attachment_id} 类型={label}({mime}) 大小={size / 1024:.1f}KB"]
    if name:
        parts.append(f"文件名={name}")
    return " ".join(parts)


def purge_conversation(*, tenant_id: str, conversation_id: str) -> int:
    """删除某个会话的全部附件文件，返回删除的文件数。

    会话被删除时调用，否则磁盘上的文件永远没人回收。
    只删「租户/会话」这一层目录，且必须落在附件根目录之内。
    """
    directory = _target_dir(tenant_id=tenant_id, conversation_id=conversation_id)
    resolved = directory.resolve()
    root = _root()

    if not resolved.is_relative_to(root) or resolved == root:
        raise AttachmentError("附件路径越界。")

    if not resolved.is_dir():
        return 0

    removed = sum(1 for item in resolved.rglob("*") if item.is_file())
    shutil.rmtree(resolved, ignore_errors=True)
    return removed


def content_digest(raw: bytes) -> str:
    """内容摘要，供幂等与去重使用。"""
    return hashlib.sha256(raw).hexdigest()[:16]


def read_text_file(path: Path) -> str:
    """读文本附件原文，不经过 OCR。

    落盘时已统一成 UTF-8（见 `_normalize_text`），这里再兜一次 GB18030：
    产物转交（`reference_service._materialize`）按原字节复制、不走那层规范化，
    一份 GBK 的 `.md` 到这里就会解码失败。

    公开出来是为了只有一份解码顺序：读取工具与产物转交都走这里，
    各写一份必然在 GBK 文件上分叉，最终表现是「同样一份文件，
    一边读得到、另一边是乱码」。
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
