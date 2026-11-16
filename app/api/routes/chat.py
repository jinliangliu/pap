# app/api/routes/chat.py
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import agent_service
from app.services.agent_service import AgentBoundary
from app.services.chat_service import ChatService
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def get_chat_service() -> ChatService:
    return ChatService()


async def resolve_agent_boundary(
    *,
    identity: Identity,
    agent_ref: str | None,
) -> AgentBoundary:
    """把请求里的 agent 解析成边界。

    放在路由层而不是 ChatService 里，是为了让「智能体不存在 / 不可用」
    保持 HTTP 语义（404 / 409）。服务层返回一个 `status="rejected"` 的
    ChatResponse 会把「选错了对象」混进「对话被拒绝」，前端没法区分。
    """
    try:
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                boundary = await agent_service.resolve_for_chat(
                    session,
                    tenant_id=identity.tenant_id,
                    ref=agent_ref,
                    actor_user_id=identity.user_id,
                )
                # 默认 Agent 是惰性创建的，这里必须提交，否则下一次请求又要建一遍。
                await session.commit()
                return boundary
    except agent_service.AgentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except agent_service.AgentUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    identity: Identity = Depends(get_current_identity),
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """
    thread_id 由客户端提供以定位会话，但身份一律取自服务端认证结果。
    客户端传入的 user_id / tenant_id / 权限字段一律忽略。
    """
    boundary = await resolve_agent_boundary(identity=identity, agent_ref=payload.agent)

    try:
        return await service.handle_message(
            identity=identity,
            thread_id=payload.thread_id,
            message=payload.message,
            # images 是旧字段名，合并进来一起处理。
            attachments=payload.merged_attachments(),
            boundary=boundary,
        )
    except RuntimeError as exc:
        # 图或运行时未初始化，属于部署问题，不伪装成业务失败。
        logger.exception("对话运行时未就绪。", extra={"event": "chat_runtime_not_ready"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="对话服务尚未就绪，请稍后重试。",
        ) from exc
