# app/api/routes/approvals.py
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.hashing import hash_parameters
from app.db.session import get_session
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditEventType
from app.schemas.approvals import (
    ApprovalBatchResponse,
    ApprovalExecutionResponse,
    ApprovalPreviewResponse,
    ConfirmApprovalRequest,
)
from app.services.approval_dependencies import get_approval_service
from app.services.approval_service import (
    ApprovalNotFoundError,
    ApprovalService,
    is_actionable,
)
from app.services.audit_service import write_audit_event
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])

TERMINAL_OK = {ApprovalStatus.VERIFIED, ApprovalStatus.EXECUTED}

# 「当前状态不允许执行」——路由文档里 409 的那个集合。
#
# 这些状态有两个入口：调用前就已经是这样（预检拦下），或者**预检通过之后才变成
# 这样**（清理任务把单子划过期、另一个标签页点了取消、另一次请求抢先占住了执行）。
# 后者原先照样回 200，于是同一个事实——「什么都没执行」——会因为清理任务有没有
# 先跑到而给出两个状态码。客户端按文档判断就会漏掉一半。
NOT_EXECUTABLE = {
    ApprovalStatus.EXPIRED,
    ApprovalStatus.REJECTED,
    ApprovalStatus.EXECUTING,
    ApprovalStatus.EXECUTION_UNKNOWN,
}


def _preview_response(approval: ApprovalRequest) -> ApprovalPreviewResponse:
    return ApprovalPreviewResponse(
        approval_id=approval.id,
        status=approval.status,
        tool_name=approval.tool_name,
        tool_display_name=approval.tool_display_name,
        risk_level=approval.risk_level,
        reason=approval.reason,
        preview=approval.preview_json or {},
        delegated_from=approval.delegated_from,
        delegation_depth=approval.delegation_depth,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
        actionable=is_actionable(approval),
        executed_at=approval.executed_at,
        error_code=approval.error_code,
    )


def _execution_response(
    approval: ApprovalRequest,
    *,
    message: str,
    success: bool | None = None,
) -> ApprovalExecutionResponse:
    """
    success 默认由审批状态推导，但冲突响应必须显式覆盖。

    一个「已完成」的单子被叫去取消，HTTP 是 409，success 却是 true，
    客户端只看字段就会得出「取消成功了」的结论。
    """
    result = approval.execution_result or {}

    if success is None:
        success = (
            True
            if approval.status in TERMINAL_OK
            else False
            if approval.status
            in {
                ApprovalStatus.FAILED,
                ApprovalStatus.REJECTED,
                ApprovalStatus.EXPIRED,
                ApprovalStatus.EXECUTION_UNKNOWN,
            }
            else None
        )

    return ApprovalExecutionResponse(
        approval_id=approval.id,
        status=approval.status,
        message=message,
        success=success,
        verified=approval.status is ApprovalStatus.VERIFIED,
        result=result.get("result"),
        verification=approval.verification,
        error_code=approval.error_code,
    )


async def _load(
    approval_id: str,
    identity: Identity,
    service: ApprovalService,
) -> ApprovalRequest:
    try:
        parsed = uuid.UUID(approval_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="approval_id 格式无效。",
        ) from exc

    try:
        return await service.get_approval(
            approval_id=parsed,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="审批记录不存在。",
        ) from exc


async def _audit_parameter_mismatch(
    *,
    session: AsyncSession,
    identity: Identity,
    approval: ApprovalRequest,
    submitted_keys: list[str],
) -> None:
    """参数与预览不一致时留痕。

    审计只是记录，不能因此改变拒绝的结论：写失败也要继续返回 422。
    """
    try:
        await write_audit_event(
            session,
            event_type=AuditEventType.APPROVAL_PARAMETER_MISMATCH,
            tenant_id=identity.tenant_id,
            actor_id=identity.user_id,
            resource_type="approval",
            resource_id=str(approval.id),
            thread_id=approval.thread_id,
            success=False,
            message="确认参数与预览不一致，已拒绝执行。",
            metadata={
                "tool_name": approval.tool_name,
                "submitted_keys": submitted_keys,
            },
        )
        await session.commit()
    except Exception:
        logger.exception(
            "参数不一致的审计写入失败。",
            extra={
                "event": "approval_mismatch_audit_failed",
                "approval_id": str(approval.id),
            },
        )
        await session.rollback()


@router.get("", response_model=ApprovalBatchResponse)
async def list_approvals(
    ids: str = Query(
        default="",
        max_length=4096,
        description="逗号分隔的审批号，用于还原历史会话里的审批卡。",
    ),
    identity: Identity = Depends(get_current_identity),
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalBatchResponse:
    """按 id 批量取审批单的当前状态。

    存在的才返回。缺的那些（格式不对、不属于本人、已被保留期清理）一律略过，
    由前端显示成「已失效」——一次批量读里混进一个失效号，不该让整段会话读不出来。

    这里不看单子当初是哪个线程开的：委派的单子开在子线程、结论落在发起方会话，
    审批人始终是同一个用户，按 (租户, 用户) 校验已经足够。
    """
    wanted: list[uuid.UUID] = []
    for raw in ids.split(","):
        text = raw.strip()
        if not text:
            continue
        try:
            wanted.append(uuid.UUID(text))
        except ValueError:
            continue

    if not wanted:
        return ApprovalBatchResponse(items=[])

    rows = await service.list_approvals(
        approval_ids=wanted,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )

    # 按请求顺序返回，让前端打日志时能对上；顺带把重复号去掉。
    order = {str(value): index for index, value in enumerate(wanted)}
    rows.sort(key=lambda row: order.get(str(row.id), len(order)))
    return ApprovalBatchResponse(items=[_preview_response(row) for row in rows])


@router.get("/{approval_id}", response_model=ApprovalPreviewResponse)
async def get_approval(
    approval_id: str,
    identity: Identity = Depends(get_current_identity),
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalPreviewResponse:
    approval = await _load(approval_id, identity, service)
    return _preview_response(approval)


@router.post("/{approval_id}/confirm", response_model=ApprovalExecutionResponse)
async def confirm_approval(
    approval_id: str,
    payload: ConfirmApprovalRequest | None = None,
    identity: Identity = Depends(get_current_identity),
    service: ApprovalService = Depends(get_approval_service),
    session: AsyncSession = Depends(get_session),
) -> ApprovalExecutionResponse | JSONResponse:
    """
    确认并执行写操作。

    服务端从不使用客户端提交的参数做执行依据：
    参数镜像只用于比对，执行使用审批记录中保存的参数。

    状态码约定：
    - 200：执行完成（或此前已完成，幂等重放）；
    - 409：当前状态不允许执行（已取消 / 已过期 / 执行中 / 结果未知）。
      客户端不能只看「有没有报错」，必须以 status 字段为准。

    409 与 200 的边界**不看清理任务有没有先跑到**：预检通过的下一秒被划成过期，
    仍然是 409。两种时序给同一个状态码，客户端才只需要实现一套分支。
    """
    approval = await _load(approval_id, identity, service)

    if approval.status in TERMINAL_OK:
        return _execution_response(
            approval,
            message="该操作此前已完成，本次不会重复执行。",
        )

    if approval.status is not ApprovalStatus.PENDING:
        # 状态冲突用 409：200 会让调用方误以为「提交成功、只是没生效」。
        conflict = _execution_response(
            approval,
            message=f"当前审批状态为 {approval.status.value}，不能执行。",
            success=False,
        )
        return JSONResponse(
            status_code=409,
            content=conflict.model_dump(mode="json"),
        )

    if payload and payload.parameters:
        if hash_parameters(payload.parameters) != approval.parameter_hash:
            # 参数与预览不一致，按安全事件留痕：只记键名，不记值。
            await _audit_parameter_mismatch(
                session=session,
                identity=identity,
                approval=approval,
                submitted_keys=sorted(payload.parameters.keys()),
            )
            raise HTTPException(
                status_code=422,
                detail="确认参数与预览内容不一致，请重新生成预览。",
            )

    chat_service = ChatService()

    try:
        result = await chat_service.resume_approval(
            identity=identity,
            thread_id=approval.thread_id,
            approval_id=str(approval.id),
            action="confirm",
            # 委派来的单子恢复的是子线程，但结论属于发起方的会话。
            persist_thread_id=approval.parent_thread_id,
            delegated=approval.is_delegated,
            # 这一轮执行要接在「发起」那次运行后面，而不是自成一条运行。
            operation_id=approval.operation_id,
            agent_name=approval.agent_name,
        )
    except Exception as exc:
        logger.warning(
            "恢复审批执行失败。",
            extra={"event": "approval_resume_failed", "approval_id": str(approval.id)},
        )
        raise HTTPException(
            status_code=409,
            detail="当前操作无法恢复执行，可能已过期或会话状态不可用。",
        ) from exc

    refreshed = await _load(approval_id, identity, service)

    if refreshed.status in NOT_EXECUTABLE:
        # 预检时还是 PENDING，执行期间才变成拒绝/过期/执行中：结论已经写进会话
        # （上面那次 resume 落过消息），但 HTTP 要按文档给 409——200 会让客户端
        # 以为「提交成功、只是没生效」，而事实是这次写入根本没发生。
        return JSONResponse(
            status_code=409,
            content=_execution_response(
                refreshed,
                message=result.answer or "当前状态不允许执行，本次不会写入。",
                success=False,
            ).model_dump(mode="json"),
        )

    return _execution_response(
        refreshed,
        message=result.answer or "已提交执行请求，请查看最终状态。",
    )


@router.post("/{approval_id}/cancel", response_model=ApprovalExecutionResponse)
async def cancel_approval(
    approval_id: str,
    identity: Identity = Depends(get_current_identity),
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalExecutionResponse | JSONResponse:
    """
    取消尚未执行的写操作。

    状态码与 confirm 保持一致：非 PENDING 一律 409。
    尤其是「已经执行完成」的单子——回 200 会让用户以为自己成功阻止了
    一次已经发生的写入，这是最不能含糊的一种误解。
    """
    approval = await _load(approval_id, identity, service)

    if approval.status in TERMINAL_OK:
        conflict = _execution_response(
            approval,
            message="该操作此前已完成并已写入，无法取消。",
            success=False,
        )
        return JSONResponse(
            status_code=409,
            content=conflict.model_dump(mode="json"),
        )

    if approval.status is not ApprovalStatus.PENDING:
        conflict = _execution_response(
            approval,
            message=f"当前审批状态为 {approval.status.value}，无需重复取消。",
            success=False,
        )
        return JSONResponse(
            status_code=409,
            content=conflict.model_dump(mode="json"),
        )

    chat_service = ChatService()

    try:
        await chat_service.resume_approval(
            identity=identity,
            thread_id=approval.thread_id,
            approval_id=str(approval.id),
            action="cancel",
            persist_thread_id=approval.parent_thread_id,
            delegated=approval.is_delegated,
            operation_id=approval.operation_id,
            agent_name=approval.agent_name,
        )
    except Exception:
        # 图状态已不可恢复时，至少把审批记录置为已拒绝。
        try:
            await service.reject(
                approval_id=approval.id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail="该操作当前无法取消。",
            ) from exc

    refreshed = await _load(approval_id, identity, service)

    return _execution_response(refreshed, message="已取消本次操作，未执行任何写入。")
