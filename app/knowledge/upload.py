# app/knowledge/upload.py
"""上传入库：REST 与 MCP 两条传输共用同一份实现。

上传不是「存一个文件」，而是一串必须同时成立的判断：后缀白名单、排除名、
分级权限、大小上限、能否解析、内容哈希是否已存在。任何一项漏掉，表现都不是
报错，而是「传上去了但检索不到」或者「同一份资料两份切片」。

两条传输各写一份的差别，只会在其中一条上出现——所以只留这一份。

两个异常对应两种处理方式，不要合并：

- ``UploadRejected`` —— 调用方能自己修正（换文件、改分级、拆小）。传输层映射 400。
- ``UploadDenied`` —— 权限不足。传输层映射 403。

两者的差别不在措辞，而在于调用方该不该重试：把 403 报成 400 会让人反复换文件，
把 400 报成 403 会让人去申请一个他本来就有的权限。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Identity
from app.core.config import settings
from app.knowledge import documents, store
from app.knowledge.index import get_knowledge_index
from app.knowledge.ingest import (
    SCOPE_REQUIREMENTS,
    SOURCE_UPLOAD,
    SUPPORTED_SUFFIXES,
    VALID_SCOPES,
    KnowledgeIngestError,
    build_document,
    extract_text_from_bytes,
    is_excluded,
)
from app.models.audit import AuditEventType
from app.services import audit_service
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

WRITE_PERMISSION = "knowledge:write"


class UploadRejected(Exception):
    """调用方能自己修正的输入问题。"""


class UploadDenied(Exception):
    """权限不足。"""


@dataclass(frozen=True)
class UploadOutcome:
    row: documents.DocumentRow
    action: str
    # 入库前的形态清洗统计（OCR 的 HTML 表转 pipe 表）。
    # 不是每个人都有：干净的 md 没有这一步，此时是空 dict。
    # 保留它是因为「入库的是 14 万字，不是你传的 56 万字」必须说得出——
    # 否则人会以为后半本没进去，或者以为自己传错了文件。
    cleanup: dict[str, int] = field(default_factory=dict)
    # 这一步产出的加工快照与原样标签。对话里要把它说成一条链路
    # （解析 → 清洗 → 结构化 → 切片 → 打标），光靠 ``row`` 说不出来：
    # 文档行只存了快照，标签是冗在各片上的。从 ``parsed`` 直接带出，
    # 免得调用方去猜 metadata_json 的形状。
    pipeline: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


def can_write_knowledge(identity: Identity) -> bool:
    return identity.is_platform_admin or WRITE_PERMISSION in identity.scopes


def precheck_upload(
    *,
    identity: Identity,
    filename: str,
    access_scope: str | None = None,
) -> str:
    """只看请求、不看字节的校验，返回归一化后的分级。

    单独抽出来是为了**先校验再读 body**：让「后缀不对」在把 5MB 读进内存
    之前就返回 400。`ingest_upload` 会再调一次——重复调用无害，
    而分头实现两份校验一定会漂移。
    """
    if not can_write_knowledge(identity):
        raise UploadDenied(f"上传文档需要 {WRITE_PERMISSION} 权限。")

    raw_name = Path(filename or "").name
    suffix = Path(raw_name).suffix.lower()

    if not raw_name:
        raise UploadRejected("缺少文件名。")
    if suffix not in SUPPORTED_SUFFIXES:
        raise UploadRejected(
            f"不支持的文件类型：{suffix or '(无扩展名)'}，"
            f"支持 {', '.join(sorted(SUPPORTED_SUFFIXES))}。"
        )
    # 与目录扫描同一套排除规则：传一个 .env 上来同样要拦。
    if is_excluded(Path(raw_name)):
        raise UploadRejected("该文件名属于禁止入库的类型。")

    scope = (access_scope or "internal").strip().lower()

    if scope not in VALID_SCOPES:
        raise UploadRejected(f"未知的分级：{scope}")

    required = SCOPE_REQUIREMENTS[scope]

    if required not in identity.scopes and not identity.is_platform_admin:
        raise UploadDenied(f"上传 {scope} 级文档需要 {required} 权限。")

    return scope


async def ingest_upload(
    *,
    session: AsyncSession,
    identity: Identity,
    filename: str,
    raw: bytes,
    title: str | None = None,
    access_scope: str | None = None,
) -> UploadOutcome:
    """校验并入库一份上传文件，返回写入后的行与动作。

    ``action`` 是 ``created`` / ``updated`` / ``unchanged``——``unchanged``
    表示同一份正文已经在库里（按内容哈希判定），此时不重复切片。
    """
    scope = precheck_upload(
        identity=identity,
        filename=filename,
        access_scope=access_scope,
    )

    raw_name = Path(filename or "").name
    limit = settings.knowledge_max_file_bytes

    if not raw:
        raise UploadRejected("文件是空的。")
    if len(raw) > limit:
        raise UploadRejected(f"文件超过 {limit // (1024 * 1024)} MB 上限。")

    cleanup: dict[str, int] = {}

    try:
        text = extract_text_from_bytes(raw, raw_name, stats=cleanup)
    except KnowledgeIngestError as exc:
        raise UploadRejected(str(exc)) from exc
    except Exception as exc:  # 损坏的 PDF/Word 不走上面的分支
        logger.warning("上传文档解析失败：%s (%s)", raw_name, exc)
        raise UploadRejected("文件无法解析，请确认没有损坏。") from exc

    # 同一份内容重复上传要幂等：换一个 logical_id 就会变成两份文档，
    # 检索时同一条资料出现两次，还会各占一份切片。
    # 只对「已就绪」的上传文档去重——下架或失败的不算已存在。
    body_hash = "sha256:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    existing = await documents.find_upload_by_hash(session, identity.tenant_id, body_hash)
    logical_id = existing.logical_id if existing else f"upload-{uuid.uuid4().hex[:12]}"
    # 标题归一交给 build_document（titles.clean_title）：这里再算一遍，
    # 两处规则就会各自演化——原本 CLI 清长尾、这里不清，就是那么来的。
    display_title = title

    try:
        parsed = build_document(
            text,
            logical_id=logical_id,
            title=display_title,
            source_uri=f"uploads/{raw_name}",
            source_type=SOURCE_UPLOAD,
            filename=raw_name,
            size_bytes=len(raw),
            meta={"access_scope": scope, "visibility": "tenant_shared"},
            acl_hint=raw_name,
            # 清洗统计一路带进流水线快照：治理页要能说出「上传 56.8 万字、
            # 入库 14 万字、压掉的都是标签」。
            pre_stats=cleanup,
        )
    except KnowledgeIngestError as exc:
        raise UploadRejected(str(exc)) from exc

    with tenant_scope(identity.tenant_id):
        action = await documents.upsert_document(
            session,
            identity.tenant_id,
            parsed,
            actor=identity.user_id,
        )
        document_id = str(store.document_uuid(identity.tenant_id, logical_id))

        if action != "unchanged":
            documents.store_upload_file(
                tenant_id=identity.tenant_id,
                document_id=document_id,
                filename=raw_name,
                raw=raw,
            )

        await store.bump_version(session, identity.tenant_id)
        await session.flush()
        await _audit(
            session,
            identity,
            document_id=document_id,
            filename=raw_name,
            size=len(raw),
            access_scope=parsed.acl["access_scope"],
        )

        row = await documents.get_document(session, identity.tenant_id, document_id)
        await session.commit()

    # 写完立刻失效本进程缓存。指纹轮询是 5 秒一次，不主动清的话
    # 「刚上传就检索」会查不到，用户只会以为上传失败。
    get_knowledge_index().invalidate(identity.tenant_id)

    if row is None:
        # 写完之后读不回来，说明写入本身有问题，不是输入问题。
        raise RuntimeError("写入后未能读回文档。")

    return UploadOutcome(
        row=row,
        action=action,
        cleanup=cleanup,
        pipeline=parsed.pipeline,
        tags=list(parsed.tags),
    )


async def _audit(
    session: AsyncSession,
    identity: Identity,
    *,
    document_id: str,
    filename: str,
    size: int,
    access_scope: str,
) -> None:
    await audit_service.write_audit_event(
        session,
        event_type=AuditEventType.ADMIN_ACTION,
        tenant_id=identity.tenant_id,
        actor_id=identity.user_id,
        resource_type="knowledge_document",
        resource_id=document_id,
        message="upload",
        metadata={
            "operation": "upload",
            "actor_subject": identity.subject,
            "filename": filename,
            "size": size,
            "access_scope": access_scope,
            "detail": "",
        },
    )
