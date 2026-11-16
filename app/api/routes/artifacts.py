# app/api/routes/artifacts.py
"""附件上传与下载。

隔离要点：

- 对象键由服务端生成并带 `tenants/<tenant>/users/<user>/` 前缀；
- 查询与下载都按 tenant_id 过滤（ORM 自动追加），跨租户一律 404；
- 下载接口不做对象存储直链：路径不是权限，必须每次经服务端校验；
- 只以附件形式返回（Content-Disposition: attachment + nosniff），
  避免上传的 HTML/SVG 在同源下被浏览器执行。
"""

from __future__ import annotations

import logging
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import get_session
from app.models.artifact import Artifact
from app.models.audit import AuditEventType
from app.schemas.artifacts import ArtifactResponse
from app.services.audit_service import write_audit_event
from app.storage.object_store import (
    ObjectNotFound,
    ObjectStoreError,
    get_object_store,
    safe_filename,
    safe_suffix,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# 这些类型即使被上传也强制按二进制下载，杜绝「上传 HTML 当页面用」。
_NEVER_INLINE = {"text/html", "text/xml", "image/svg+xml", "application/javascript"}
_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")


def _to_response(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=str(artifact.id),
        filename=artifact.filename,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        conversation_id=artifact.conversation_id,
        created_at=artifact.created_at,
    )


def _safe_content_type(raw: str | None) -> str:
    value = (raw or "").split(";")[0].strip().lower()

    if not _CONTENT_TYPE.match(value):
        return "application/octet-stream"

    return value


def _download_type(stored: str) -> str:
    return "application/octet-stream" if stored in _NEVER_INLINE else stored


@router.put("", response_model=ArtifactResponse)
async def upload_artifact(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    conversation_id: str | None = Query(default=None, max_length=255),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> ArtifactResponse:
    """以原始请求体上传。使用 PUT + 原始 body，避免引入 multipart 依赖。"""
    declared = request.headers.get("content-length")

    if declared and declared.isdigit() and int(declared) > settings.artifact_max_bytes:
        raise HTTPException(status_code=413, detail="文件超过平台上限。")

    buffer = bytearray()

    async for chunk in request.stream():
        buffer.extend(chunk)

        if len(buffer) > settings.artifact_max_bytes:
            raise HTTPException(status_code=413, detail="文件超过平台上限。")

    if not buffer:
        raise HTTPException(status_code=400, detail="文件内容为空。")

    display_name = safe_filename(filename)
    artifact_id = uuid.uuid4()
    key = get_object_store().build_key(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        artifact_id=artifact_id,
        suffix=safe_suffix(display_name),
    )

    size, digest = await get_object_store().put(key, bytes(buffer))

    artifact = Artifact(
        id=artifact_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        conversation_id=conversation_id,
        object_key=key,
        filename=display_name,
        content_type=_safe_content_type(request.headers.get("content-type")),
        size_bytes=size,
        sha256=digest,
    )

    try:
        async with session.begin():
            session.add(artifact)
            await session.flush()

            await write_audit_event(
                session,
                event_type=AuditEventType.ARTIFACT_UPLOADED,
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="artifact",
                resource_id=str(artifact.id),
                request_id=request.headers.get("X-Request-ID"),
                metadata={"size_bytes": size, "content_type": artifact.content_type},
            )
    except Exception:
        # 元数据没写成功就把对象删掉，避免留下「不可达但占空间」的文件。
        await get_object_store().delete(key)
        raise

    return _to_response(artifact)


@router.get("", response_model=list[ArtifactResponse])
async def list_artifacts(
    limit: int = Query(default=50, ge=1, le=200),
    conversation_id: str | None = Query(
        default=None,
        max_length=255,
        description="只列这个会话的产物。不传 = 列当前用户全部产物。",
    ),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> list[ArtifactResponse]:
    """列产物。按 `conversation_id` 过滤是「按会话保留」的读路径。

    不过滤时会把用户历史上传的通用材料也列出来（它们的 `conversation_id`
    是空），那正是「我手里有哪些文件」要回答的。传了这个参数就只看这一场
    对话里生成的东西——换话题之后找回「刚才那张表」走的就是这条路。
    """
    statement = select(Artifact).where(
        Artifact.tenant_id == identity.tenant_id,
        Artifact.user_id == identity.user_id,
    )

    if conversation_id is not None:
        statement = statement.where(Artifact.conversation_id == conversation_id)

    rows = (
        await session.scalars(
            statement.order_by(Artifact.created_at.desc()).limit(limit)
        )
    ).all()

    return [_to_response(row) for row in rows]


async def _load_owned(
    *,
    artifact_id: str,
    identity: Identity,
    session: AsyncSession,
) -> Artifact:
    try:
        parsed = uuid.UUID(artifact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="附件不存在。") from exc

    artifact = await session.scalar(
        select(Artifact).where(
            Artifact.id == parsed,
            Artifact.tenant_id == identity.tenant_id,
            Artifact.user_id == identity.user_id,
        )
    )

    if artifact is None:
        raise HTTPException(status_code=404, detail="附件不存在。")

    # 双保险：键必须落在当前租户/用户的前缀下。
    if not get_object_store().key_belongs_to(
        artifact.object_key,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    ):
        logger.error(
            "附件对象键与归属不一致，已拒绝。",
            extra={"event": "artifact_key_mismatch", "artifact_id": str(artifact.id)},
        )
        raise HTTPException(status_code=404, detail="附件不存在。")

    return artifact


@router.get("/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    artifact_id: str,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> ArtifactResponse:
    artifact = await _load_owned(
        artifact_id=artifact_id,
        identity=identity,
        session=session,
    )
    return _to_response(artifact)


@router.get("/{artifact_id}/content")
async def download_artifact(
    artifact_id: str,
    request: Request,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    artifact = await _load_owned(
        artifact_id=artifact_id,
        identity=identity,
        session=session,
    )

    try:
        data = await get_object_store().read(artifact.object_key)
    except ObjectNotFound as exc:
        raise HTTPException(status_code=404, detail="附件内容不存在。") from exc
    except ObjectStoreError as exc:
        logger.warning(
            "读取附件失败。",
            extra={"event": "artifact_read_failed", "artifact_id": str(artifact.id)},
        )
        raise HTTPException(status_code=500, detail="附件读取失败。") from exc

    await write_audit_event(
        session,
        event_type=AuditEventType.ARTIFACT_DOWNLOADED,
        tenant_id=identity.tenant_id,
        actor_id=identity.user_id,
        resource_type="artifact",
        resource_id=str(artifact.id),
        request_id=request.headers.get("X-Request-ID"),
        metadata={"size_bytes": artifact.size_bytes},
    )
    await session.commit()

    disposition = (
        "attachment; filename*=UTF-8''" + quote(artifact.filename, safe="")
    )

    return Response(
        content=data,
        media_type=_download_type(artifact.content_type),
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
