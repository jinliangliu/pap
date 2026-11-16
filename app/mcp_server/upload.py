# app/mcp_server/upload.py
"""入向通道的文件上传端点：`POST /mcp/upload`。

## 为什么不是一个 MCP 工具

MCP 工具的参数是 JSON，传文件只能 base64 内联。对**人**没问题，对**模型**是死路：
调用方模型必须逐 token 生成那串 base64，一个 169KB 的文档就是二十多万字符，
既生成不出来，也没人愿意为它付上下文。所以文件必须由**有字节的进程**搬运，
端点收 multipart 才是对的形态。

工具面上仍然只有 `pap_delegate` + `pap_task_status` 两个，理由和 P0 一致：
按能力列工具等于让上层替 PAP 规划。

## 为什么挂在 `/mcp` 下而不是 `/api/v1`

`/api/v1` 的凭据来自网关注入的身份头（浏览器面，Basic 认证）。把 PAT 接进
`get_current_identity` 会让一枚令牌直接等于「全站控制台权限」——而 PAT 的
设计前提正是「只开一个窄口子」。所以这里沿用 `BearerGate` 那一套：
同一枚令牌、同一套身份解析、同一个 `Identity`，但**出口仍然只有这一个端点**。

授权不看令牌，看人：`knowledge:write` 或平台管理员，与 REST 上传同一个判据——
两处都用 `app/knowledge/upload.py` 里的那一份。
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge.upload import (
    UploadDenied,
    UploadRejected,
    can_write_knowledge,
    ingest_upload,
)
from app.mcp_server.auth import STATE_KEY
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

# 与 `BearerGate` 的 prefix 对齐：多一个路径段，由 `server.build_asgi` 分流过来。
UPLOAD_PATH = "/mcp/upload"

_MESSAGES = {
    "created": "已入库《{title}》：{chunks} 片 · 分级 {scope}。",
    "updated": "已更新《{title}》：{chunks} 片 · 分级 {scope}。",
    "unchanged": "《{title}》内容没有变化，已存在（{chunks} 片），没有重复入库。",
}


def is_upload_path(path: str) -> bool:
    return path.rstrip("/") == UPLOAD_PATH


async def handle_upload(scope: dict, receive: Any, send: Any) -> None:
    """处理一次上传。鉴权已在 `BearerGate` 里做完。"""
    request = Request(scope, receive)

    if request.method != "POST":
        await _respond(
            scope,
            receive,
            send,
            {"error": "method_not_allowed", "detail": "上传只接受 POST。"},
            status_code=405,
            headers={"Allow": "POST"},
        )
        return

    identity = _identity_of(scope)

    if identity is None:
        # 门槛已经挡过一轮；走到这里说明挂载方式变了。仍然 fail-closed。
        await _respond(
            scope,
            receive,
            send,
            {"error": "unauthorized", "detail": "需要有效的 Bearer 令牌。"},
            status_code=401,
        )
        return

    if not can_write_knowledge(identity):
        await _respond(
            scope,
            receive,
            send,
            {
                "error": "forbidden",
                "detail": "上传文档需要 knowledge:write 权限或平台管理员身份。",
            },
            status_code=403,
        )
        return

    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        await _respond(
            scope,
            receive,
            send,
            {
                "error": "bad_request",
                "detail": "请用 multipart/form-data 上传，文件字段名是 file。",
            },
            status_code=400,
        )
        return

    try:
        form = await request.form(max_files=1, max_fields=8)
    except Exception as exc:  # 截断的 multipart、缺 boundary 都走这里
        logger.warning(
            "上传请求解析失败：%s (%s)",
            type(exc).__name__,
            exc,
            extra={"event": "mcp_upload_bad_form"},
        )
        await _respond(
            scope,
            receive,
            send,
            {
                "error": "bad_request",
                "detail": "表单无法解析，请确认是完整的 multipart 请求。",
            },
            status_code=400,
        )
        return

    upload = form.get("file")
    raw = await _read_bytes(upload)

    if raw is None:
        await _respond(
            scope,
            receive,
            send,
            {
                "error": "bad_request",
                "detail": "缺少文件，或文件为空、超过大小上限。",
            },
            status_code=400,
        )
        return

    try:
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                outcome = await ingest_upload(
                    session=session,
                    identity=identity,
                    filename=getattr(upload, "filename", None) or "",
                    raw=raw,
                    title=_clean(form.get("title")),
                    access_scope=_clean(form.get("access_scope")),
                )
    except UploadDenied as exc:
        await _respond(
            scope, receive, send,
            {"error": "forbidden", "detail": str(exc)},
            status_code=403,
        )
        return
    except UploadRejected as exc:
        await _respond(
            scope, receive, send,
            {"error": "rejected", "detail": str(exc)},
            status_code=400,
        )
        return
    except RuntimeError as exc:
        logger.error(
            "上传写入后读不回。",
            extra={"event": "mcp_upload_unverified", "detail": str(exc)},
        )
        await _respond(
            scope, receive, send,
            {"error": "internal", "detail": str(exc)},
            status_code=500,
        )
        return

    row = outcome.row
    message = _MESSAGES.get(outcome.action, _MESSAGES["created"]).format(
        title=row.title,
        chunks=row.chunk_count,
        scope=row.access_scope,
    )

    logger.info(
        "MCP 通道入库完成。",
        extra={
            "event": "mcp_upload_ok",
            "document_id": str(row.id),
            "action": outcome.action,
            "chunks": row.chunk_count,
            "size": len(raw),
        },
    )

    await _respond(
        scope,
        receive,
        send,
        {
            "document": {
                "id": str(row.id),
                "title": row.title,
                "filename": row.filename,
                "chunk_count": row.chunk_count,
                "access_scope": row.access_scope,
                "status": row.status,
                "source_type": row.source_type,
            },
            "action": outcome.action,
            "size_bytes": len(raw),
            "message": message,
        },
        status_code=200,
    )


def _identity_of(scope: dict) -> Identity | None:
    state = scope.get("state") or {}
    cached = state.get(STATE_KEY) if isinstance(state, dict) else None
    return cached if isinstance(cached, Identity) else None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


async def _read_bytes(upload: Any) -> bytes | None:
    """把上传文件读进内存，边读边判上限。

    不信任客户端声明的长度：multipart 里的 size 由客户端写，
    以实际读到的字节数为准。
    """
    if upload is None or not hasattr(upload, "read"):
        return None

    limit = settings.knowledge_max_file_bytes
    buffer = bytearray()

    while True:
        chunk = await upload.read(1 << 20)

        if not chunk:
            break

        buffer.extend(chunk)

        if len(buffer) > limit:
            logger.warning(
                "上传超过大小上限。",
                extra={"event": "mcp_upload_too_large", "limit": limit},
            )
            return None

    return bytes(buffer) if buffer else None


async def _respond(
    scope: dict,
    receive: Any,
    send: Any,
    payload: dict,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> None:
    response = JSONResponse(payload, status_code=status_code, headers=headers)
    await response(scope, receive, send)
