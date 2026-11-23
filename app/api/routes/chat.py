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
from app.services.chat_service import (
    BUILD_HANDOFF_MESSAGE,
    ChatService,
    is_build_command,
)
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
    # `/build` 是界面的快捷方式，不是平台命令。放到最前面（连边界都不解析）：
    # 这条消息不该创建默认 Agent、不该占配额、不该在会话里留一行。
    #
    # 为什么必须在这里拒、而不是放给模型：它不是 `/workflow`，会当成普通消息
    # 发给模型，而模型会非常配合地回一句「已帮你把流程建好」——它没有那个工具，
    # 只是在说一件听起来对的话。承诺了没做的事，用户要等到去编排页才发现。
    if is_build_command(payload.message):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CHAT_BUILD_HANDOFF", "message": BUILD_HANDOFF_MESSAGE},
        )

    # 两个都传等于「这句话既交给这个人、又交给这条流程」：流程的每一步用的是
    # 节点自带的智能体，`agent` 在这里没有任何作用。与其静默丢掉一个，
    # 不如说清楚——「我选了它，它却换了一个人」是最难排查的那种。
    if payload.workflow and payload.agent:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "CHAT_TARGET_CONFLICT",
                "message": "一条消息只能选一个对象：智能体，或一条流程。",
            },
        )

    # 选了流程就不解析智能体边界：流程节点各自带智能体，这里只需一个基线，
    # 免得为一次流程触发顺手创建出租户的默认 Agent。
    boundary = (
        agent_service.BASELINE
        if payload.workflow
        else await resolve_agent_boundary(identity=identity, agent_ref=payload.agent)
    )

    try:
        return await service.handle_message(
            identity=identity,
            thread_id=payload.thread_id,
            message=payload.message,
            # images 是旧字段名，合并进来一起处理。
            attachments=payload.merged_attachments(),
            boundary=boundary,
            workflow=payload.workflow,
        )
    except RuntimeError as exc:
        # 图或运行时未初始化，属于部署问题，不伪装成业务失败。
        logger.exception("对话运行时未就绪。", extra={"event": "chat_runtime_not_ready"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="对话服务尚未就绪，请稍后重试。",
        ) from exc
