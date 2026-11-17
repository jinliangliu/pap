# app/tools/ocr_tools.py
"""附件文字读取工具：图片 / PDF 走 OCR，Markdown / 纯文本直接读原文。

识别能力**不在平台内**：本机 OCR 服务以 MCP（Streamable HTTP）暴露
`ocr_file(file_path)`，平台把附件句柄解析成绝对路径后调它。

为什么还留一层 `ocr_document` 包装，而不是把 `ocr_file` 直接注册给模型：

- 模型不该知道服务器路径。直接暴露 `ocr_file` 等于给模型一个
  「读任意文件」的入口，OCR 服务那边没有任何目录约束；
- 附件句柄（`img_/doc_/txt_`）到路径的解析、租户/会话目录校验、
  公网 URL 的 SSRF 白名单、大小上限、旧句柄识别，全在平台这一侧；
- 工具名与参数（`file_id` / `file_url`）保持不变，
  权限、审批、审计、观测、历史会话的观察记录都不用跟着改。

文本附件为什么也挂在这个名字下，而不是另起一个 `read_text_attachment`：
对模型来说动作只有一个——「把这份附件的文字给我」。分成两个工具，
它得先判断文件类型再选工具，选错一次就是一轮白跑（对着 `.md` 调 OCR 服务）。
名字里的 ocr 是历史包袱，描述里写清了真实能力，代价比多一个入口小。

代价是要求 OCR 服务与平台**同机**（或共享挂载同一目录）：传的是路径不是字节。
`file_url` 会先下载到临时文件再传路径，用完删掉。

早期版本叫 `ocr_image`，只支持图片；加入 PDF 后改名 `ocr_document`。
名字必须跟着能力走——叫 image 却吞 PDF，模型会按名字判断用途。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.integrations.mcp_client import McpError, McpToolClient
from app.models.approval import RiskLevel
from app.services import attachment_store
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

_URL_MAX_BYTES = 10 * 1024 * 1024
_URL_TIMEOUT_SECONDS = 10.0

# 工具名与旧名。旧名只用于识别历史会话里的观察记录，不再注册。
TOOL_NAME = "ocr_document"
LEGACY_TOOL_NAME = "ocr_image"
OBSERVATION_TOOL_NAMES = frozenset({TOOL_NAME, LEGACY_TOOL_NAME})

# 远端的识别工具名。OCR 服务只暴露这一个工具，写死比可配置安全：
# 这层包装的职责就是把「一个受控的入口」接到「一个固定的能力」上。
REMOTE_TOOL_NAME = "ocr_file"

# 远端把自己的异常也当成正常文本返回（形如 `Error: ...`）。
# 不识别出来，模型会把「PDF support not installed」当成文件内容念给用户。
_ERROR_PREFIX = "Error:"


class _UnsafeUrlError(RuntimeError):
    pass


class OcrUnavailableError(RuntimeError):
    """OCR 没配好，或者根本连不上：属于「运维问题」，模型重试也没用。"""


class OcrFailedError(RuntimeError):
    """OCR 服务可达但这次识别失败：换个文件或稍后重试有意义。"""


def _assert_public_host(host: str) -> None:
    """拒绝私网/环回/链路本地等非公网地址，阻断 SSRF。"""
    if not host:
        raise _UnsafeUrlError("附件地址缺少主机名。")

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise _UnsafeUrlError("无法解析附件地址的主机名。") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise _UnsafeUrlError("附件地址指向内网或保留地址，已拒绝。")


async def _fetch_remote_file(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise _UnsafeUrlError("只支持 http/https 地址。")

    _assert_public_host(parsed.hostname or "")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_URL_TIMEOUT_SECONDS),
        follow_redirects=False,
    ) as client:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise _UnsafeUrlError(f"下载附件失败：{exc}") from exc

    if response.status_code != 200:
        raise _UnsafeUrlError(f"下载附件失败：HTTP {response.status_code}")

    raw = response.content
    if len(raw) > _URL_MAX_BYTES:
        raise _UnsafeUrlError("附件超过大小上限。")

    mime = attachment_store.sniff_mime(raw)
    if mime is None:
        raise _UnsafeUrlError(
            "地址返回的内容既不是受支持的图片，也不是 PDF；"
            "Markdown / 文本请作为附件上传后按 file_id 读取。"
        )

    return raw, mime


def _resolve_local_file(
    *,
    context: ToolExecutionContext,
    file_id: str,
) -> tuple[Path, str]:
    """把附件句柄解析成平台自己的绝对路径，顺带做大小上限校验。

    不再读字节：识别在 OCR 进程里做，平台只传路径。
    """
    if not context.conversation_id:
        raise attachment_store.AttachmentError("当前会话上下文缺失，无法定位附件。")

    path, mime = attachment_store.resolve_attachment(
        tenant_id=context.tenant_id,
        conversation_id=context.conversation_id,
        attachment_id=file_id,
    )

    limit = attachment_store.max_bytes_for(
        mime,
        max_image_bytes=settings.ocr_max_image_bytes,
        max_pdf_bytes=settings.ocr_max_pdf_bytes,
    )
    if path.stat().st_size > limit:
        raise attachment_store.AttachmentError("附件超过大小上限。")
    return path, mime


def _spool_to_temp(raw: bytes, mime: str) -> Path:
    """把下载来的字节落到临时文件。

    OCR 服务按**扩展名**分流引擎（图片 / PDF），所以后缀必须与真实内容一致，
    不能用固定的 `.bin`；文件也不放进附件根目录——那是给会话附件用的，
    往里面塞临时文件会让 purge 的统计对不上。
    """
    handle, name = tempfile.mkstemp(
        prefix="ocr-", suffix=attachment_store.suffix_for(mime)
    )
    with os.fdopen(handle, "wb") as fp:
        fp.write(raw)
    return Path(name)


async def _call_ocr_mcp(file_path: Path, *, timeout: float | None = None) -> str:
    """调 OCR MCP 的 `ocr_file`，返回识别文本。

    单独抽成模块级函数是为了让测试只替换这一层：
    路径解析、大小校验、错误分类、截断都在本文件其它地方，可以照测。
    """
    url = (settings.ocr_mcp_url or "").strip()
    if not url:
        raise OcrUnavailableError(
            "未配置 OCR 服务地址（OCR_MCP_URL），附件文字识别不可用。"
        )

    # 空串与 None 语义不同：None = 该端点不鉴权，连 Authorization 头都不发。
    token = (settings.ocr_mcp_token or "").strip() or None

    client = McpToolClient(
        url=url,
        token=token,
        connect_timeout_seconds=5.0,
        call_timeout_seconds=float(timeout or settings.ocr_mcp_timeout_seconds),
    )

    try:
        payload = await client.call_tool(
            remote_tool_name=REMOTE_TOOL_NAME,
            arguments={"file_path": str(file_path)},
        )
    except McpError as exc:
        # 连不上/超时/地址写错都属于部署问题，明确说清比「稍后重试」有用。
        raise OcrUnavailableError(f"OCR 服务不可用：{exc}") from exc

    text = _text_of(payload)
    if text.startswith(_ERROR_PREFIX):
        raise OcrFailedError(text[len(_ERROR_PREFIX) :].strip() or "OCR 服务返回失败。")
    return text


def _text_of(payload: dict[str, Any]) -> str:
    """从 MCP 返回里取出文本。

    `ocr_file` 声明返回 str，但 FastMCP 对 str 的封装方式随版本变过
    （纯 TextContent / structured_content），几种形状都认，取不到就当空。
    """
    for key in ("text", "result", "value", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _read_text_file(path: Path) -> str:
    """直接读文本附件原文，不经过 OCR。

    解码顺序（UTF-8 → GB18030）只有一份，放在 `attachment_store.read_text_file`：
    产物转交（`reference_service`）按原字节复制、不走落盘时的规范化，
    两边各写一份解码顺序必然在 GBK 文件上分叉。
    """
    return attachment_store.read_text_file(path)


# 截断之后该走哪条路。写在工具结果里，而不是只写在系统提示词里——
# 模型对「工具刚告诉它的下一步」比对齐一整段规则更服从。
# 提示词连说三遍「被截断时不要反问」，它照样回「这份文档被截断了，入库方式
# 需要你定一下」；把同一句话放进读取结果，它才开始照着说。
# 2026-11-17 起「整篇入库」的落点是构建助手的 `studio_import`（按磁盘原文切片），
# 不再让人去侧栏找入口——那个入口已经合并进对话框。
_TRUNCATED_NEXT_STEP = (
    "整篇内容无法在一次读取中拿到，也就无法据此整篇写入知识库。"
    "需要整篇入库时，请让用户到构建助手里上传这份文件并说明用途——"
    "那边按原文切片导入，不受单次读取上限影响；"
    "只处理其中几章就按章节分批读取、逐章处理"
)


def _truncate_text(text: str, *, keep_tail: bool = False) -> tuple[str, bool]:
    """按字符数截断并明说，不能让模型以为一份长文档就这么点内容。

    `keep_tail` 给文本附件（尤其是日志）用：出问题的那几行几乎总在末尾，
    只留开头会出现「日志读了两千行，结论是启动正常」——而它启动之后就崩了。
    图片 / PDF 仍只留开头：那是按页顺序读的文档，读到最后几页没有特别价值。
    """
    limit = settings.ocr_max_text_chars
    if len(text) <= limit:
        return text, False

    if keep_tail:
        head = limit * 7 // 10
        tail = limit - head
        omitted = len(text) - limit
        return (
            text[:head]
            + f"\n\n（原文共 {len(text)} 字符，超出单次上限 {limit}，中间 {omitted} 字符已截断；"
            f"这里保留了开头 {head} 与结尾 {tail} 字符。"
            + _TRUNCATED_NEXT_STEP
            + "）\n\n"
            + text[-tail:],
            True,
        )

    return (
        text[:limit]
        + f"\n\n（原文共 {len(text)} 字符，超出单次上限 {limit}，后文已截断；"
        + _TRUNCATED_NEXT_STEP
        + "）",
        True,
    )


async def ocr_document(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    file_id = (arguments.get("file_id") or "").strip() or None
    file_url = (arguments.get("file_url") or "").strip() or None

    if bool(file_id) == bool(file_url):
        return ToolExecutionResult(
            success=False,
            message="必须且只能提供 file_id 或 file_url 其中之一。",
            error_code="INVALID_ARGUMENTS",
        )

    temp_path: Path | None = None

    try:
        if file_id:
            path, mime = _resolve_local_file(context=context, file_id=file_id)
            source = {"file_id": file_id}
        else:
            raw, mime = await _fetch_remote_file(str(file_url))
            temp_path = _spool_to_temp(raw, mime)
            path = temp_path
            source = {"file_url": str(file_url)}
    except attachment_store.AttachmentError as exc:
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="ATTACHMENT_NOT_AVAILABLE",
        )
    except _UnsafeUrlError as exc:
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="ATTACHMENT_URL_REJECTED",
        )

    kind = attachment_store.kind_of(mime)
    # 文本附件不送 OCR：原文就是答案，过一道识别引擎只会引入错字与耗时。
    inline = mime in attachment_store.TEXT_MIMES

    try:
        if inline:
            text = _read_text_file(path)
        else:
            text = await _call_ocr_mcp(path)
    except OSError as exc:
        logger.warning(
            "读取附件文件失败。",
            extra={"event": "attachment_read_failed", "error": str(exc)[:300]},
        )
        return ToolExecutionResult(
            success=False,
            message=f"读取附件失败：{exc}",
            error_code="ATTACHMENT_NOT_AVAILABLE",
        )
    except OcrUnavailableError as exc:
        logger.warning(
            "OCR 服务不可用。",
            extra={"event": "ocr_unavailable", "error": str(exc)[:300]},
        )
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="OCR_NOT_CONFIGURED",
        )
    except OcrFailedError as exc:
        logger.warning(
            "OCR 识别失败。",
            extra={"event": "ocr_failed", "error": str(exc)[:300]},
        )
        return ToolExecutionResult(
            success=False,
            message=f"OCR 识别失败：{exc}",
            retryable=True,
            error_code="OCR_SERVICE_ERROR",
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    # 文本附件保留结尾：日志与追踪记录的关键一行在最后，只留开头等于没读到。
    text, truncated = _truncate_text(text.strip(), keep_tail=inline)

    payload: dict[str, Any] = {
        **source,
        "kind": kind,
        "mime": mime,
        "provider": "inline_text" if inline else "ocr_mcp",
        "text": text,
        "line_count": len(text.splitlines()),
    }
    if truncated:
        payload["truncated"] = True

    # 句柄能在本会话解析，就说明它是这个会话的材料：附件随会话保留，
    # 用户上一轮传的文件这一轮照样能读，这是正常用法，不是抄错句柄。
    # 这里只标出「不是本条消息新增的」，让模型和用户都能看出这一条读的是
    # 更早的材料，避免把内容安到这次的问题头上。
    earlier = bool(
        file_id and context.attachment_ids and file_id not in context.attachment_ids
    )

    if not text:
        message = "附件中没有文字内容。"
    elif kind == "text":
        message = f"已读取文本附件原文，共 {payload['line_count']} 行。"
    elif kind == "pdf":
        message = f"已识别 PDF 文字，共 {payload['line_count']} 行。"
    else:
        message = f"已识别图片文字，共 {payload['line_count']} 行。"

    if earlier:
        payload["from_previous_message"] = True
        message = (
            "注意：这个附件不在本会话当前的材料清单里，可能来自更早的会话。"
            + message
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result=payload,
        message=message,
        verification={
            "verified": True,
            "reason": "text_read" if inline else "ocr_completed",
        },
    )


def build_ocr_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=TOOL_NAME,
            display_name="读取附件文字",
            description=(
                "读取附件里的文字并返回纯文本：图片与 PDF 走 OCR，"
                "文本类文件（Markdown、纯文本、日志、JSON / YAML 等配置）直接返回原文。"
                "当用户上传了附件（上下文 [附件] 中给出 file_id），"
                "且需要读取其中的内容、编号、金额、日期、表头或单据信息时调用。"
                "长文本会保留开头与结尾，中间截断并注明。"
                "纯文本对话不要调用。不要凭猜测描述附件内容：先调用本工具拿到文字。"
                "file_id 必须原样取自 [附件] 里的值（本条新增的，或本会话此前上传、"
                "仍然可读的材料），不要编造，也不要凭文件名猜句柄。"
                "也支持对公网 http/https 的图片或 PDF 地址识别。"
            ),
            kind="http",
            risk_level=RiskLevel.READ,
            requires_approval=False,
            timeout_seconds=120,
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "pattern": r"^(img|doc|txt)_[0-9a-f]{12}$",
                        "description": "上下文 [附件] 中给出的附件句柄。",
                    },
                    "file_url": {
                        "type": "string",
                        "description": "公网可访问的图片或 PDF 地址（与 file_id 二选一）。",
                    },
                },
                "additionalProperties": False,
            },
            handler=ocr_document,
            verification_hint="文字由 OCR 服务或附件原文直接返回，未做人工核验。",
        )
    ]


def as_langchain_tool(context: ToolExecutionContext):
    """把 ocr_document 暴露成原生 LangChain Tool。

    仅用于平台执行图之外的场合（脚本、LangChain Agent、Studio 自定义节点）。

    与走 ToolGateway 的区别必须清楚：
    - 本适配器**不经过**审计、限流、租户工具策略、审批链路；
    - 只有附件路径约束（file_id 白名单 + 根目录校验）和 SSRF 防护仍然生效；
    - 因此 context 必须由可信调用方构造，不能让模型或外部请求自由指定。

    平台内的对话链路**不应**使用本函数，
    它走的是 `ToolGateway.execute_read_only`，那里才有完整管控。
    """
    from langchain_core.tools import StructuredTool

    async def _call(
        file_id: str | None = None,
        file_url: str | None = None,
    ) -> dict[str, Any]:
        result = await ocr_document(
            context,
            {"file_id": file_id or "", "file_url": file_url or ""},
        )

        if not result.success:
            return {
                "ok": False,
                "error_code": result.error_code,
                "message": result.message,
            }

        return {"ok": True, **result.result}

    return StructuredTool.from_function(
        coroutine=_call,
        name=TOOL_NAME,
        description=build_ocr_tools()[0].description,
    )
