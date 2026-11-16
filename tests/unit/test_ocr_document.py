# tests/unit/test_ocr_document.py
"""ocr_document 工具：句柄解析、图片/PDF 分流、SSRF 防护、失败分类。

识别本身在 OCR 的 MCP 服务里，这里只测平台这一侧：
句柄 → 路径、大小上限、错误归类、截断，以及「远端报错别当成正文」。
所以统一替换 `ocr_tools._call_ocr_mcp` 这一层，其余代码照真实跑。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import attachment_store
from app.tools import ocr_tools
from app.tools.types import ToolExecutionContext

# 1x1 PNG（真实文件头，可通过 MIME 嗅探）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# 只需要文件头能通过嗅探；工具本身不解析 PDF 结构。
_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"

# 一份小 Markdown：内容本身不重要，重要的是「没有文件头也能被认出是文本」。
_MD = "# 巡检规程\n\n1. 每日检查调压箱压力。\n".encode()


def _data_url(raw: bytes, declared: str = "") -> str:
    """构造成浏览器可能给出的样子：`.md` 的 type 常常是空串。"""
    head = f"data:{declared};base64" if declared else "data:;base64"
    return head + "," + base64.b64encode(raw).decode()


def _context(
    conversation_id: str = "conv-1",
    attachment_ids: tuple[str, ...] = (),
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="default",
        user_id="u1",
        thread_id="t1",
        run_id="r1",
        request_id="req1",
        conversation_id=conversation_id,
        attachment_ids=attachment_ids,
    )


def _fake_mcp(text: str):
    """装上假的 OCR 调用，并把每次收到的路径记下来。"""
    seen: list[Path] = []

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:
        seen.append(file_path)
        assert file_path.exists(), "传给 OCR 的路径必须真实存在"
        return text

    return caller, seen


def _store(tmp_path, monkeypatch, *, raw: bytes, mime: str, conversation_id="conv-1") -> str:
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))
    record = attachment_store.save_bytes(
        tenant_id="default",
        conversation_id=conversation_id,
        raw=raw,
        mime=mime,
    )
    return record.attachment_id


def _store_image(tmp_path, monkeypatch) -> str:
    return _store(tmp_path, monkeypatch, raw=_PNG, mime="image/png")


def _store_pdf(tmp_path, monkeypatch) -> str:
    return _store(tmp_path, monkeypatch, raw=_PDF, mime="application/pdf")


async def test_requires_exactly_one_source():
    missing = await ocr_tools.ocr_document(_context(), {})
    assert missing.success is False
    assert missing.error_code == "INVALID_ARGUMENTS"

    both = await ocr_tools.ocr_document(
        _context(),
        {"file_id": "img_0123456789ab", "file_url": "https://example.com/a.png"},
    )
    assert both.success is False
    assert both.error_code == "INVALID_ARGUMENTS"


async def test_image_happy_path_passes_server_path_to_mcp(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)
    caller, seen = _fake_mcp("站点编号 T002509008")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.verified is True
    assert result.result["text"] == "站点编号 T002509008"
    assert result.result["provider"] == "ocr_mcp"
    assert result.result["file_id"] == file_id
    assert result.result["kind"] == "image"
    assert "page_count" not in result.result
    # 传的是平台自己的附件绝对路径，不是字节，也不是用户给的字符串
    assert seen[0].is_absolute()
    assert seen[0].suffix == ".png"
    assert seen[0].is_relative_to(Path(tmp_path).resolve())


async def test_pdf_happy_path(tmp_path, monkeypatch):
    file_id = _store_pdf(tmp_path, monkeypatch)
    caller, seen = _fake_mcp("甲\n乙")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.result["kind"] == "pdf"
    assert result.result["line_count"] == 2
    assert "PDF" in result.message
    # 后缀必须跟着真实类型走：OCR 服务按扩展名分流引擎
    assert seen[0].suffix == ".pdf"


async def test_empty_text_is_not_a_failure(tmp_path, monkeypatch):
    """扫描件识别不出字是正常结果，不能记成失败——否则会触发重试风暴。"""
    file_id = _store_pdf(tmp_path, monkeypatch)
    caller, _ = _fake_mcp("")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.result["text"] == ""
    assert "没有文字内容" in result.message


async def test_long_text_is_truncated_and_says_so(tmp_path, monkeypatch):
    """MCP 只回一段纯文本，页边界没了，只能用字符数兜住上下文。"""
    file_id = _store_pdf(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "ocr_max_text_chars", 1000)
    caller, _ = _fake_mcp("字" * 1200)
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.result["truncated"] is True
    assert "已截断" in result.result["text"]
    # 截断后要明确告诉模型「后面还有」，不能让它以为文件就这么长
    assert "1200" in result.result["text"]





async def test_rejects_malformed_file_id():
    result = await ocr_tools.ocr_document(_context(), {"file_id": "../../etc/passwd"})
    assert result.success is False
    assert result.error_code == "ATTACHMENT_NOT_AVAILABLE"


async def test_rejects_attachment_from_other_conversation(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:  # pragma: no cover
        raise AssertionError("越权句柄不应触发识别")

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context("conv-2"), {"file_id": file_id})
    assert result.success is False
    assert result.error_code == "ATTACHMENT_NOT_AVAILABLE"


async def test_missing_conversation_context_is_rejected():
    context = _context("")
    result = await ocr_tools.ocr_document(context, {"file_id": "img_0123456789ab"})
    assert result.success is False
    assert result.error_code == "ATTACHMENT_NOT_AVAILABLE"


async def test_rejects_private_url(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(ocr_tools.socket, "getaddrinfo", fake_getaddrinfo)

    result = await ocr_tools.ocr_document(
        _context(), {"file_url": "http://internal.local/a.pdf"}
    )
    assert result.success is False
    assert result.error_code == "ATTACHMENT_URL_REJECTED"


async def test_ocr_unavailable_is_reported_as_not_configured(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:
        raise ocr_tools.OcrUnavailableError("未配置 OCR 服务地址（OCR_MCP_URL）")

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})
    assert result.success is False
    assert result.error_code == "OCR_NOT_CONFIGURED"


async def test_ocr_failure_is_retryable(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:
        raise ocr_tools.OcrFailedError("Unsupported file type: .png")

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})
    assert result.success is False
    assert result.error_code == "OCR_SERVICE_ERROR"
    assert result.retryable is True


def test_tool_definition_is_readonly_and_not_approval_gated():
    tool = ocr_tools.build_ocr_tools()[0]

    assert tool.name == "ocr_document"
    assert tool.requires_approval is False
    assert tool.risk_level.value == "read"
    assert tool.station_id_argument is None
    # 名字必须跟着能力走：早期叫 ocr_image 只认图片，后来吞了 PDF。
    # 现在图片 / PDF / Markdown / 纯文本都读，名字就不能停在某一种格式上。
    assert "附件" in tool.display_name
    assert "Markdown" in tool.description
    # 模型必须能看懂「什么时候该用」，否则会把 OCR 当成万能工具
    assert "file_id" in tool.description
    # 参数名也换了：旧字段 image_id 已不存在，仍留在 schema 里会误导模型
    assert set(tool.input_schema["properties"]) == {"file_id", "file_url"}
    # 文本附件的句柄是 txt_ 前缀；schema 不认，模型传了会被本地校验直接拒
    assert "txt" in tool.input_schema["properties"]["file_id"]["pattern"]


def test_sniff_rejects_non_attachment():
    with pytest.raises(attachment_store.AttachmentError):
        attachment_store.decode_data_url(
            "data:image/png;base64," + base64.b64encode(b"not an image").decode(),
            max_image_bytes=1024,
        )


def test_declared_mime_is_overridden_by_content():
    """声明成图片、内容是 PDF 时必须按 PDF 处理，否则会送错识别引擎。"""
    data_url = "data:image/png;base64," + base64.b64encode(_PDF).decode()

    raw, mime = attachment_store.decode_data_url(
        data_url, max_image_bytes=1024, max_pdf_bytes=1024 * 1024
    )

    assert raw == _PDF
    assert mime == attachment_store.PDF_MIME


def test_pdf_has_its_own_size_limit():
    """PDF 限制与图片限制分开，不能用图片上限卡死多页 PDF。"""
    payload = _PDF + b"x" * 2048
    data_url = "data:application/pdf;base64," + base64.b64encode(payload).decode()

    # 图片上限足够小，但 PDF 走自己的上限，应当通过
    raw, mime = attachment_store.decode_data_url(
        data_url, max_image_bytes=1024, max_pdf_bytes=1024 * 1024
    )
    assert mime == attachment_store.PDF_MIME

    with pytest.raises(attachment_store.AttachmentError):
        attachment_store.decode_data_url(
            data_url, max_image_bytes=1024, max_pdf_bytes=1024
        )


async def test_markdown_attachment_is_read_verbatim(tmp_path, monkeypatch):
    """文本附件不送 OCR：原文就是答案，过一道识别只会引入错字与延迟。"""
    file_id = _store(
        tmp_path, monkeypatch, raw=_MD, mime=attachment_store.MARKDOWN_MIME
    )

    async def boom(*_args, **_kwargs):
        raise AssertionError("文本附件不该调用 OCR 服务")

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", boom)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.result["kind"] == "text"
    assert result.result["provider"] == "inline_text"
    assert result.result["text"].startswith("# 巡检规程")
    assert "原文" in result.message
    # 句柄前缀区分类型：txt_ 是文本，img_/doc_ 是图片与 PDF
    assert file_id.startswith("txt_")


def test_markdown_without_declared_type_is_accepted(tmp_path, monkeypatch):
    """浏览器对 `.md` 常常给空 type：判定只能靠后缀，不能因为「没声明」就拒。"""
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))

    record = attachment_store.save_attachment(
        tenant_id="default",
        conversation_id="conv-1",
        data_url=_data_url(_MD),
        max_image_bytes=1024 * 1024,
        max_pdf_bytes=1024 * 1024,
        name="巡检规程.md",
    )

    assert record.kind == "text"
    assert record.mime == attachment_store.MARKDOWN_MIME

    path, mime = attachment_store.resolve_attachment(
        tenant_id="default",
        conversation_id="conv-1",
        attachment_id=record.attachment_id,
    )
    assert mime == attachment_store.MARKDOWN_MIME
    assert path.read_bytes() == _MD


def test_binary_disguised_as_markdown_is_rejected():
    """改名不是通行证：`.docx` 改成 `.md` 仍是二进制，必须拒。"""
    zip_header = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 32

    with pytest.raises(attachment_store.AttachmentError):
        attachment_store.decode_data_url(
            _data_url(zip_header, "application/octet-stream"),
            max_image_bytes=1024 * 1024,
            name="伪装.md",
        )


def test_gbk_markdown_is_normalized_to_utf8():
    """GBK 的 `.md` 落盘前统一转 UTF-8：否则读出来是乱码，还像「原文就这样」。"""
    raw, mime = attachment_store.decode_data_url(
        _data_url("调压箱巡检".encode("gbk"), "application/octet-stream"),
        max_image_bytes=1024 * 1024,
        name="说明.md",
    )

    assert mime == attachment_store.MARKDOWN_MIME
    assert raw.decode("utf-8") == "调压箱巡检"


def test_text_attachment_has_its_own_size_limit():
    """文本上限与图片/PDF 分开：4 MB 文本远小于 PDF 上限，不能被 PDF 上限放宽。"""
    big = ("字" * (attachment_store._TEXT_MAX_BYTES // 3 + 100)).encode()

    with pytest.raises(attachment_store.AttachmentError):
        attachment_store.decode_data_url(
            _data_url(big),
            max_image_bytes=1024 * 1024,
            max_pdf_bytes=64 * 1024 * 1024,
            name="太大.md",
        )


@pytest.mark.parametrize(
    ("name", "declared", "body"),
    [
        ("run.log", "text/plain", "2026-11-16 10:00 ERROR 连接超时\n"),
        ("trace.jsonl", "", '{"stage": "plan", "ok": false}\n'),
        ("agent.yaml", "text/yaml", "name: field-ops\ncan_write: true\n"),
        ("settings.ini", "application/octet-stream", "[mcp]\nenabled = true\n"),
    ],
)
def test_log_and_config_files_are_accepted(name, declared, body):
    """构建助手的文本附件有三种用途：建库、排查、新增配置。

    按用途挑后缀等于让用户先猜「我的文件算哪一类」，所以日志与配置
    必须和 `.md` 一样收；判定仍只有一道——后缀在白名单 + 内容像文本。
    """
    raw, mime = attachment_store.decode_data_url(
        _data_url(body.encode(), declared),
        max_image_bytes=1024 * 1024,
        name=name,
    )

    assert mime in attachment_store.TEXT_MIMES
    assert raw.decode("utf-8") == body


def test_declared_json_mime_is_normalized_to_text():
    """浏览器给 `.json` 报 `application/json` 是合法的，不能当成「不支持的类型」。

    归一化成 text/plain 很重要：`kind_of()` 只认两个规范 MIME，
    留着 application/json 会让附件被判成图片、进而送去 OCR。
    """
    raw, mime = attachment_store.decode_data_url(
        _data_url(b'{"a": 1}', "application/json"),
        max_image_bytes=1024 * 1024,
        name="config",  # 没有后缀，只能靠声明
    )

    assert mime == attachment_store.TEXT_MIME
    assert attachment_store.kind_of(mime) == "text"


def test_binary_disguised_as_log_is_rejected():
    """日志后缀同样不是通行证：二进制改名成 `.log` 仍然拒。"""
    zip_header = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 32

    with pytest.raises(attachment_store.AttachmentError):
        attachment_store.decode_data_url(
            _data_url(zip_header, "application/octet-stream"),
            max_image_bytes=1024 * 1024,
            name="伪装.log",
        )


async def test_long_text_keeps_head_and_tail(tmp_path, monkeypatch):
    """长日志要留结尾：出问题的那几行在最后，只留开头等于没读到。

    2026-11-16：一份 2MB 的运行日志被截到前 4 万字符，结论是「启动正常」——
    而它启动完之后崩了，崩在哪一行正好在被截掉的部分。
    """
    body = "启动正常\n" + ("填充行\n" * 400) + "ERROR 最后一行崩了\n"
    file_id = _store(
        tmp_path, monkeypatch, raw=body.encode(), mime=attachment_store.TEXT_MIME
    )
    monkeypatch.setattr(settings, "ocr_max_text_chars", 200)

    result = await ocr_tools.ocr_document(_context(), {"file_id": file_id})

    assert result.success is True
    assert result.result["truncated"] is True
    text = result.result["text"]
    assert text.startswith("启动正常")
    assert text.rstrip().endswith("ERROR 最后一行崩了")
    assert "已截断" in text


async def test_langchain_adapter_returns_structured_payload(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)
    caller, _ = _fake_mcp("适配器文本")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    tool = ocr_tools.as_langchain_tool(_context())
    assert tool.name == "ocr_document"

    payload = await tool.ainvoke({"file_id": file_id})
    assert payload["ok"] is True
    assert payload["text"] == "适配器文本"


async def test_langchain_adapter_reports_failure_without_raising(tmp_path, monkeypatch):
    file_id = _store_image(tmp_path, monkeypatch)

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:
        raise ocr_tools.OcrUnavailableError("未启用")

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    payload = await ocr_tools.as_langchain_tool(_context()).ainvoke({"file_id": file_id})
    assert payload["ok"] is False
    assert payload["error_code"] == "OCR_NOT_CONFIGURED"


async def test_handle_outside_conversation_materials_is_flagged(tmp_path, monkeypatch):
    """句柄不在本会话的材料清单里时要标明来源，不能当成这次要读的那份。

    清单随 state 下传（本条新增 + 本会话此前上传），正常情况下历史材料的句柄
    也在其中；落在这之外的，只能是模型拼错了或指的是别的会话——解析可能
    仍然成功，所以必须在结果里说清楚，否则用户看到的是「识别错了文件」。
    """
    image_id = _store_image(tmp_path, monkeypatch)
    caller, _ = _fake_mcp("调压箱/柜现场记录表")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    # 清单里只有 PDF，模型却拿了一个不在清单里的图片句柄
    context = _context(attachment_ids=("doc_0123456789ab",))

    result = await ocr_tools.ocr_document(context, {"file_id": image_id})

    assert result.success is True
    assert result.result["from_previous_message"] is True
    assert "不在本会话当前的材料清单里" in result.message
    assert "调压箱/柜现场记录表" in result.result["text"]


async def test_handle_from_current_message_is_not_flagged(tmp_path, monkeypatch):
    image_id = _store_image(tmp_path, monkeypatch)
    caller, _ = _fake_mcp("本次附件")
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    context = _context(attachment_ids=(image_id,))

    result = await ocr_tools.ocr_document(context, {"file_id": image_id})

    assert result.success is True
    assert "from_previous_message" not in result.result
    assert "不在本会话当前的材料清单里" not in result.message


# ---------------------------------------------------------------- MCP 这一层


async def test_missing_mcp_url_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(settings, "ocr_mcp_url", "")

    with pytest.raises(ocr_tools.OcrUnavailableError) as excinfo:
        await ocr_tools._call_ocr_mcp(Path("/tmp/whatever.png"))

    assert "OCR_MCP_URL" in str(excinfo.value)


async def test_transport_failure_becomes_unavailable(monkeypatch):
    """连不上/超时是部署问题，不该让模型以为是文件本身有问题。"""
    from app.integrations.mcp_client import McpUnavailableError

    async def boom(self, *, remote_tool_name, arguments):  # noqa: ANN001, ARG001
        raise McpUnavailableError("MCP 服务不可达：ConnectError。")

    monkeypatch.setattr(ocr_tools.McpToolClient, "call_tool", boom)
    monkeypatch.setattr(settings, "ocr_mcp_url", "http://127.0.0.1:4499/mcp")

    with pytest.raises(ocr_tools.OcrUnavailableError) as excinfo:
        await ocr_tools._call_ocr_mcp(Path("/tmp/whatever.png"))

    assert "不可用" in str(excinfo.value)


async def test_call_tool_passes_absolute_path_and_no_token(monkeypatch):
    """OCR 服务不鉴权：不带 Authorization 头；地址与工具名走常量。"""
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

        async def call_tool(self, *, remote_tool_name, arguments):  # noqa: ANN001
            captured["remote"] = remote_tool_name
            captured["args"] = arguments
            return {"text": "识别文本"}

    monkeypatch.setattr(ocr_tools, "McpToolClient", FakeClient)
    monkeypatch.setattr(settings, "ocr_mcp_url", "http://127.0.0.1:4499/mcp")
    monkeypatch.setattr(settings, "ocr_mcp_token", None)

    text = await ocr_tools._call_ocr_mcp(Path("/data/uploads/a.png"))

    assert text == "识别文本"
    assert captured["url"] == "http://127.0.0.1:4499/mcp"
    assert captured["token"] is None
    assert captured["remote"] == "ocr_file"
    assert captured["args"] == {"file_path": str(Path("/data/uploads/a.png"))}


def test_text_of_accepts_several_envelopes():
    """FastMCP 对 str 的封装随版本变过，几种形状都要认。"""
    assert ocr_tools._text_of({"text": "a"}) == "a"
    assert ocr_tools._text_of({"result": "b"}) == "b"
    assert ocr_tools._text_of({"value": "c"}) == "c"
    assert ocr_tools._text_of({}) == ""


async def test_file_url_is_spooled_to_temp_and_cleaned_up(monkeypatch):
    """公网地址要先落成带正确后缀的临时文件，用完删掉。"""
    seen: list[Path] = []

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return _PDF, attachment_store.PDF_MIME

    async def caller(file_path: Path, *, timeout: float | None = None) -> str:
        seen.append(file_path)
        assert file_path.exists()
        return "远程 PDF 文本"

    monkeypatch.setattr(ocr_tools, "_fetch_remote_file", fake_fetch)
    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", caller)

    result = await ocr_tools.ocr_document(
        _context(), {"file_url": "https://example.com/a.pdf"}
    )

    assert result.success is True
    assert result.result["kind"] == "pdf"
    assert result.result["file_url"] == "https://example.com/a.pdf"
    assert seen[0].suffix == ".pdf"
    assert not seen[0].exists(), "临时文件必须清理，否则每轮对话都在 /tmp 留垃圾"


async def test_remote_error_string_is_not_returned_as_text(monkeypatch):
    """OCR 服务把异常当正常文本返回（`Error: ...`），不能念给用户当正文。

    远端 `ocr_file` 捕获异常后 return "Error: ..."，协议层是 success 的；
    平台必须自己认出来，否则「PDF 引擎没装」会被当成文件内容念给用户。
    """

    class FakeClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        async def call_tool(self, *, remote_tool_name, arguments):  # noqa: ANN001
            return {"text": "Error: PDF support not installed"}

    monkeypatch.setattr(ocr_tools, "McpToolClient", FakeClient)
    monkeypatch.setattr(settings, "ocr_mcp_url", "http://127.0.0.1:4499/mcp")

    with pytest.raises(ocr_tools.OcrFailedError) as excinfo:
        await ocr_tools._call_ocr_mcp(Path("/tmp/a.pdf"))

    assert "PDF support not installed" in str(excinfo.value)
