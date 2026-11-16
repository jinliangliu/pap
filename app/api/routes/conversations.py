# app/api/routes/conversations.py
"""对话台的会话历史。

边界：
- 只返回「当前身份 + 当前租户」自己的会话，路径参数不参与身份判断，
  也没有任何按 id 跨用户读取的入口；`channel` 只是在这个范围里再收窄，
  不收窄时与不带该参数逐字等价；
- 内容就是 messages 表里的原始记录，不做摘要、不做跨会话聚合；
- 删除是**销毁**，不是隐藏：业务记录、运行记录里的原话摘要、LangGraph 图状态、
  用户上传的原始文件与会话产物一并清掉。只删 conversations 与 messages 的话，
  agent_runs 和 checkpoint 里仍是完整对话，等于没删（见 conversation_purge）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.artifact import Artifact
from app.models.context import ConversationSummary as ContextSummaryRow
from app.models.conversation import AgentRun, Conversation, Message
from app.schemas.conversation import (
    ConversationAttachment,
    ConversationContextSummary,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMessage,
    ConversationRenameRequest,
    ConversationSummary,
)
from app.services import attachment_store
from app.services.conversation_purge import purge_conversation_traces
from app.tenancy.context import tenant_scope
from app.tenancy.threads import client_thread_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _summary(row: Conversation, *, message_count: int = 0) -> ConversationSummary:
    return ConversationSummary(
        id=row.id,
        thread_id=client_thread_id(row.thread_id),
        title=(row.title or "").strip() or "未命名会话",
        channel=row.channel or "api",
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        message_count=message_count,
    )


def _attachments_of(row: Conversation, metadata: dict) -> list[ConversationAttachment]:
    """从消息元数据里还原附件清单。

    只回填结构完整的条目：历史消息可能来自没有附件功能的版本，
    或者写坏了半条记录，缺字段的直接跳过，不拿默认值凑。
    """
    items: list[ConversationAttachment] = []

    for item in (metadata or {}).get("attachments") or []:
        if not isinstance(item, dict):
            continue

        attachment_id = str(item.get("attachment_id") or "")
        mime = str(item.get("mime") or "")

        if not attachment_id or not mime:
            continue

        items.append(
            ConversationAttachment(
                attachment_id=attachment_id,
                kind=str(item.get("kind") or attachment_store.kind_of(mime)),
                mime=mime,
                size=int(item.get("size") or 0),
                name=item.get("name") or None,
                url=f"/api/v1/conversations/{row.id}/attachments/{attachment_id}",
            )
        )

    return items


async def _load_owned(
    session: AsyncSession,
    *,
    identity: Identity,
    conversation_id: uuid.UUID,
) -> Conversation:
    """按 id + tenant + user 三条件取会话。少一个条件都算越权。"""
    row = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == identity.tenant_id,
            Conversation.user_id == identity.user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return row


def _owned_conditions(identity: Identity, channel: str | None) -> list:
    """列表与计数共用的过滤条件。

    两处各写一遍必然走散：改了一处忘了另一处，现象是「列表只有 3 条、
    总数写着 20」——看着像分页坏了，其实是条件不一致。
    """
    conditions = [
        Conversation.tenant_id == identity.tenant_id,
        Conversation.user_id == identity.user_id,
    ]
    if channel:
        conditions.append(Conversation.channel == channel)
    return conditions


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    channel: str | None = Query(
        default=None,
        max_length=32,
        description="按入口过滤。studio = 构建助手面板开的会话，api = 其余",
    ),
    identity: Identity = Depends(get_current_identity),
) -> ConversationListResponse:
    """当前用户的会话列表，按最近活跃倒序。

    `channel` 只是**收窄**当前用户自己的会话，不改变可见范围：
    不带时与原来逐字一致，带 `studio` 时看不见别人的、也看不见别的入口的。
    """
    conditions = _owned_conditions(identity, channel)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            total = await session.scalar(
                select(func.count(Conversation.id)).where(*conditions)
            )

            rows = (
                await session.execute(
                    select(Conversation, func.count(Message.id))
                    .outerjoin(Message, Message.conversation_id == Conversation.id)
                    .where(*conditions)
                    .group_by(Conversation.id)
                    .order_by(Conversation.last_active_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()

    return ConversationListResponse(
        items=[_summary(row, message_count=count) for row, count in rows],
        total=int(total or 0),
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> ConversationDetailResponse:
    """取单个会话及其全部消息（按时间正序）。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load_owned(
                session, identity=identity, conversation_id=conversation_id
            )

            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == row.id)
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            ).all()

            summaries = (
                await session.scalars(
                    select(ContextSummaryRow)
                    .where(ContextSummaryRow.conversation_id == row.id)
                    .order_by(
                        ContextSummaryRow.created_at.asc(),
                        ContextSummaryRow.id.asc(),
                    )
                )
            ).all()

    return ConversationDetailResponse(
        conversation=_summary(row, message_count=len(messages)),
        messages=[
            ConversationMessage(
                id=item.id,
                role=item.role.value if hasattr(item.role, "value") else str(item.role),
                content=item.content,
                metadata=item.metadata_json or {},
                attachments=_attachments_of(row, item.metadata_json or {}),
                created_at=item.created_at,
            )
            for item in messages
        ],
        summaries=[
            ConversationContextSummary(
                id=item.id,
                through_message_id=item.through_message_id,
                turns_covered=item.turns_covered,
                messages_covered=item.messages_covered,
                summary=item.summary,
                tokens_before=item.tokens_before,
                tokens_after=item.tokens_after,
                created_at=item.created_at,
            )
            for item in summaries
        ],
    )


@router.get("/{conversation_id}/attachments/{attachment_id}")
async def get_conversation_attachment(
    conversation_id: uuid.UUID,
    attachment_id: str,
    identity: Identity = Depends(get_current_identity),
) -> FileResponse:
    """取回会话里的附件原文件，供前端显示缩略图与 PDF 预览。

    鉴权与列表、详情完全一致：先按 id + tenant + user 确认会话归属，
    再按 (租户, 会话) 定位文件。别人的 attachment_id 拿到也是 404——
    句柄本身不是凭据。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load_owned(
                session, identity=identity, conversation_id=conversation_id
            )
            resolved_id = str(row.id)

    try:
        path, mime = attachment_store.resolve_attachment(
            tenant_id=identity.tenant_id,
            conversation_id=resolved_id,
            attachment_id=attachment_id,
        )
    except attachment_store.AttachmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return FileResponse(
        path,
        media_type=mime,
        # inline 才能让浏览器内嵌预览；用 attachment 会直接变成下载。
        headers={
            "Content-Disposition": (
                f'inline; filename="{attachment_id}{attachment_store.suffix_for(mime)}"'
            ),
            # 附件内容按句柄不变，可以放心让浏览器缓存；但只能是私有缓存。
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.patch("/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationRenameRequest,
    identity: Identity = Depends(get_current_identity),
) -> ConversationSummary:
    """重命名会话。只影响标题，不触碰消息内容。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await _load_owned(
                    session, identity=identity, conversation_id=conversation_id
                )
                row.title = payload.title.strip()
                await session.flush()

            count = await session.scalar(
                select(func.count(Message.id)).where(Message.conversation_id == row.id)
            )

    return _summary(row, message_count=int(count or 0))


@router.delete("/{conversation_id}", status_code=status.HTTP_200_OK)
async def delete_conversation(
    conversation_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> dict:
    """销毁会话：业务记录、运行记录、图状态、附件文件，全都不保留。不可恢复。"""
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await _load_owned(session, identity=identity, conversation_id=conversation_id)

                internal_thread = row.thread_id

                # 一会话一线程是当前的设计，但删之前仍要确认：万一有别的会话
                # 复用了同一个 thread_id，删图状态会把那份还在用的上下文一起抹掉。
                shared_thread = await session.scalar(
                    select(func.count(Conversation.id)).where(
                        Conversation.thread_id == internal_thread,
                        Conversation.id != row.id,
                    )
                )

                # 产物的对象键要在这里取出来：记录删掉之后就无处可查了，
                # 而文件清理必须等事务提交后再做（见下方注释）。
                artifact_scope = {
                    str(row.id),
                    str(conversation_id),
                    internal_thread,
                    client_thread_id(internal_thread),
                }
                artifact_keys = list(
                    (
                        await session.scalars(
                            select(Artifact.object_key).where(
                                Artifact.tenant_id == identity.tenant_id,
                                Artifact.conversation_id.in_(artifact_scope),
                            )
                        )
                    ).all()
                )

                # 显式删消息：不依赖数据库是否开启 ON DELETE CASCADE。
                await session.execute(delete(Message).where(Message.conversation_id == row.id))

                # 运行记录不能只靠外键的 SET NULL：那会把「谁属于这个会话」抹掉，
                # 却把 input_summary / output_summary 里的原话留在库里。
                await session.execute(
                    delete(AgentRun).where(
                        AgentRun.tenant_id == identity.tenant_id,
                        AgentRun.conversation_id == row.id,
                    )
                )

                if artifact_keys:
                    await session.execute(
                        delete(Artifact).where(
                            Artifact.tenant_id == identity.tenant_id,
                            Artifact.conversation_id.in_(artifact_scope),
                        )
                    )

                await session.delete(row)

    logger.info(
        "会话已删除。",
        extra={
            "event": "conversation_deleted",
            "conversation_id": str(conversation_id),
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "artifacts": len(artifact_keys),
        },
    )

    # 图状态与磁盘文件放在 DB 提交之后：事务回滚时记录还在，文件却删了就再也
    # 恢复不了；反过来最多是短时间内多占一点磁盘，可以接受。
    report = await purge_conversation_traces(
        tenant_id=identity.tenant_id,
        conversation_id=str(conversation_id),
        thread_id=None if shared_thread else internal_thread,
        artifact_keys=artifact_keys,
    )

    logger.info(
        "会话痕迹已清理。",
        extra={
            "event": "conversation_purged",
            "conversation_id": str(conversation_id),
            "tenant_id": identity.tenant_id,
            "checkpoints_purged": report.checkpoints,
            "thread_shared": bool(shared_thread),
            "artifact_files": report.artifact_files,
            "attachment_files": report.attachment_files,
            "failures": report.failures,
        },
    )

    return {"deleted": True, "id": str(conversation_id)}
