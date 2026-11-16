# app/services/approval_service.py
"""审批生命周期管理。

写操作不得从 LangGraph 节点或 API Router 直接调用 Gateway：

    错误：gateway.execute_approved(...)
    正确：approval_service.confirm_and_execute(...)
              → 锁定审批记录
              → 校验状态/归属/过期时间
              → 比对参数哈希
              → 标记 EXECUTING 并提交
              → Gateway 执行（携带幂等键）
              → 回读核验
              → 落最终状态 + 审计

同一审批只允许执行一次：已 EXECUTED / VERIFIED / EXECUTING 的记录不会再次调用外部系统。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.hashing import hash_parameters
from app.models.agent import Agent, AgentStatus
from app.models.approval import (
    ApprovalRequest,
    ApprovalStatus,
)
from app.models.audit import AuditEventType
from app.repositories.approval_repository import ApprovalRepository
from app.services.agent_service import (
    AgentBoundary,
    AgentUnavailable,
    agent_subset,
    boundary_of,
    describe_violation,
)
from app.services.audit_service import write_audit_event
from app.services.run_service import close_runs_for_approval
from app.tools.gateway import (
    ToolDisabledError,
    ToolGateway,
    ToolPermissionDeniedError,
    ToolResourceDeniedError,
)
from app.tools.types import (
    ToolCallPlan,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """审批流程基础异常。"""


class ApprovalNotFoundError(ApprovalError):
    """审批记录不存在，或不属于当前用户。"""


class ApprovalStateError(ApprovalError):
    """审批状态不允许当前操作。"""


class ApprovalExpiredError(ApprovalError):
    """审批预览已过期。"""


class ApprovalParameterMismatchError(ApprovalError):
    """确认时传入的参数与预览参数不一致。"""


class DelegationRevoked(ApprovalError):
    """批准期间委派链不再成立，这张单子不该执行。

    与 `AgentUnavailable` 分开：那个说的是「干活的人没了」，
    这个说的是「把他叫来的那条链没了」——B 自己可能一点没变，
    变的是它的上游。两者的排障方向完全不同，不能合成一个错误码。
    """


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_actionable(approval: ApprovalRequest, *, now: datetime | None = None) -> bool:
    """这张单子现在还能不能点。

    界面上「显示确认按钮」与「点下去真的会执行」必须是同一个判断，所以只在
    这里定义一次。前端据此渲染，服务端确认时据此拒绝，两边不会各说各话。

    **过期也算不可点**：状态可能还写着 PENDING，只是清理任务还没跑到。
    这时候继续给按钮，点下去只会拿到 409——比直接说「已过期」差得多。

    用服务端时钟而不是浏览器时钟：客户端差几分钟，一张还能确认的单子就会
    显示成过期，反过来更糟。
    """
    if approval.status is not ApprovalStatus.PENDING:
        return False
    return _as_utc(approval.expires_at) > (now or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ApprovalExecutionResult:
    approval_id: uuid.UUID
    status: ApprovalStatus
    message: str

    tool_result: ToolExecutionResult | None = None
    already_processed: bool = False
    error_code: str | None = None

    @property
    def success(self) -> bool:
        return self.status in {
            ApprovalStatus.EXECUTED,
            ApprovalStatus.VERIFIED,
        }

    @property
    def verified(self) -> bool:
        return self.status is ApprovalStatus.VERIFIED


class ApprovalService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        tool_gateway: ToolGateway,
        default_expire_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._tool_gateway = tool_gateway
        self._default_expire_seconds = default_expire_seconds

    # ---------------- 创建 ----------------

    async def create_approval(
        self,
        *,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        run_id: str,
        plan: ToolCallPlan,
        preview: dict[str, Any],
        expire_seconds: int | None = None,
        idempotency_key: str | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        agent_revision: int | None = None,
        agent_allowed_tools: list[str] | None = None,
        agent_can_write: bool | None = None,
        delegation_chain: list[dict[str, str | None]] | None = None,
        parent_thread_id: str | None = None,
        operation_id: str | None = None,
    ) -> ApprovalRequest:
        """
        创建待确认审批。

        idempotency_key 用于抵抗 LangGraph 的节点重入：
        interrupt 恢复时节点会从头重跑，同一 (thread_id, run_id) 必须复用
        同一条审批记录，否则会生成孤儿审批。

        `delegation_chain` 为空表示这张单子由用户直接对话的那个 Agent 开出；
        非空表示它是某一跳委派的产物，执行时要额外的逐跳复核（见
        `_review_delegation`）。`parent_thread_id` 只在委派时给：子调用跑在
        独立的子线程上，而结论要回到发起方所在的会话。
        """
        now = datetime.now(UTC)
        effective = expire_seconds or self._default_expire_seconds

        if effective <= 0:
            raise ValueError("expire_seconds 必须大于 0。")

        key = idempotency_key or f"approval:{uuid.uuid4()}"

        if idempotency_key:
            async with self._session_factory() as session:
                existing = await session.scalar(
                    select(ApprovalRequest).where(
                        ApprovalRequest.idempotency_key == key
                    )
                )

            if existing is not None:
                return existing

        approval = ApprovalRequest(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            # 边界快照：执行时与当前边界取交集，收窄立即生效、放宽不生效。
            agent_id=agent_id,
            agent_name=agent_name,
            agent_revision=agent_revision,
            agent_allowed_tools=agent_allowed_tools,
            agent_can_write=agent_can_write,
            delegation_chain=list(delegation_chain) if delegation_chain else None,
            parent_thread_id=parent_thread_id,
            # 记下这张单子属于哪一次运行：确认后是新的 trace，只能从这里继承，
            # 否则「发起」与「执行」会变成两条互不相干的运行。
            operation_id=operation_id,
            tool_name=plan.tool_name,
            tool_display_name=plan.display_name,
            risk_level=plan.risk_level,
            status=ApprovalStatus.PENDING,
            reason=plan.reason,
            parameters_json=plan.arguments,
            parameter_hash=hash_parameters(plan.arguments),
            preview_json=preview,
            idempotency_key=key,
            created_at=now,
            expires_at=now + timedelta(seconds=effective),
            execution_attempts=0,
        )

        async with self._session_factory() as session:
            async with session.begin():
                await ApprovalRepository(session).create(approval)
                await write_audit_event(
                    session,
                    event_type=AuditEventType.APPROVAL_CREATED,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    resource_type="approval",
                    resource_id=str(approval.id),
                    thread_id=thread_id,
                    run_id=run_id,
                    metadata={
                        "tool_name": plan.tool_name,
                        "risk_level": plan.risk_level.value,
                        "parameter_hash": approval.parameter_hash,
                        "expires_at": approval.expires_at.isoformat(),
                        # 「谁批的」之外还要能答「谁要做的」：同一工具，
                        # 默认 Agent 发起和只读 Agent 发起是两件不同的事。
                        "agent_name": agent_name or None,
                        "agent_revision": agent_revision,
                        # 「是不是被转交来的」。只在委派时出现，非委派单不留空键：
                        # 审计里 `delegation` 存在与否本身就是判断依据。
                        **(
                            {
                                "delegation": {
                                    "chain": [
                                        {"id": hop.get("id"), "name": hop.get("name")}
                                        for hop in (delegation_chain or [])
                                    ],
                                    "depth": len(delegation_chain or []),
                                    "parent_thread_id": parent_thread_id,
                                }
                            }
                            if delegation_chain
                            else {}
                        ),
                    },
                )

        return approval

    # ---------------- 查询 / 拒绝 ----------------

    async def get_approval(
        self,
        *,
        approval_id: uuid.UUID,
        tenant_id: str,
        user_id: str,
    ) -> ApprovalRequest:
        async with self._session_factory() as session:
            approval = await ApprovalRepository(session).get_for_user(
                approval_id=approval_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        if approval is None:
            raise ApprovalNotFoundError("未找到可操作的审批记录。")

        return approval

    async def list_approvals(
        self,
        *,
        approval_ids: list[uuid.UUID],
        tenant_id: str,
        user_id: str,
    ) -> list[ApprovalRequest]:
        """批量取审批单，用于把历史会话里留下的审批号还原成卡片。

        存在的才返回，缺的都当「已失效」——与仓储层同样的理由：不能因为
        一条历史记录过期被清理，就让整段会话读不出来。
        """
        async with self._session_factory() as session:
            return await ApprovalRepository(session).list_for_user(
                approval_ids=approval_ids,
                tenant_id=tenant_id,
                user_id=user_id,
            )

    async def reject(
        self,
        *,
        approval_id: uuid.UUID,
        tenant_id: str,
        user_id: str,
    ) -> ApprovalRequest:
        async with self._session_factory() as session:
            async with session.begin():
                repository = ApprovalRepository(session)
                approval = await repository.get_for_update(
                    approval_id=approval_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )

                if approval is None:
                    raise ApprovalNotFoundError("未找到可操作的审批记录。")

                if approval.status is not ApprovalStatus.PENDING:
                    raise ApprovalStateError(
                        f"当前审批状态为 {approval.status.value}，不能再取消。"
                    )

                approval.status = ApprovalStatus.REJECTED
                approval.completed_at = datetime.now(UTC)

                await write_audit_event(
                    session,
                    event_type=AuditEventType.APPROVAL_REJECTED,
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    resource_type="approval",
                    resource_id=str(approval.id),
                    thread_id=approval.thread_id,
                    run_id=approval.run_id,
                    metadata={"tool_name": approval.tool_name},
                )

                # 事务提交之后再收口运行：放在事务里会让「运行已结束」比
                # 「审批已取消」先被人看见，中间那一瞬是两个状态自相矛盾。
                approval_ref = str(approval.id)
                approval_run_id = approval.run_id or None
                approval_tenant_id = approval.tenant_id

        await close_runs_for_approval(
            approval_id=approval_ref,
            run_id=approval_run_id,
            tenant_id=approval_tenant_id,
            status=ApprovalStatus.REJECTED,
        )

        return approval

    # ---------------- 确认并执行 ----------------

    async def _execution_agent_boundary(
        self, approval: ApprovalRequest
    ) -> tuple[tuple[str, ...] | None, bool]:
        """执行时的有效边界 = 审批时快照 ∩ 当前配置。

        **收窄立即生效，放宽不生效。** 审批与执行跨两次请求，中间可能过了几分钟：
        期间把只读 Agent 放开成可写，一张按只读批准的写单不该因此照跑；
        反过来，期间把一个工具从 Agent 的允许清单里删掉，就该立刻拦住。

        无 agent_id（存量审批单 / 平台基线）时只看快照，行为与改造前一致。
        """
        snapshot_allowed = (
            None
            if approval.agent_allowed_tools is None
            else tuple(approval.agent_allowed_tools)
        )
        snapshot_write = (
            True if approval.agent_can_write is None else bool(approval.agent_can_write)
        )

        if not approval.agent_id:
            # 委派单一定带 agent_id（子调用的上下文里有 B 的 id）。缺了说明这行
            # 数据不完整，此时「按快照放行」等于跳过复核——宁可拒绝也不要静默。
            if approval.delegation_chain:
                raise DelegationRevoked(
                    "该审批记录缺少执行方标识，无法复核委派链，本次不会执行。"
                )
            return snapshot_allowed, snapshot_write

        agent: Agent | None = None
        try:
            agent_id = uuid.UUID(approval.agent_id)
        except (ValueError, AttributeError, TypeError):
            agent_id = None

        if agent_id is not None:
            async with self._session_factory() as session:
                agent = await session.get(Agent, agent_id)

        if agent is None:
            raise AgentUnavailable("发起该操作的智能体已被删除，本次不会执行。")
        if agent.status != AgentStatus.ACTIVE:
            raise AgentUnavailable(
                f"智能体「{agent.display_name or agent.name}」已停用，本次不会执行。"
            )

        current_allowed = (
            None if agent.allowed_tools is None else tuple(agent.allowed_tools)
        )

        if snapshot_allowed is None:
            effective = current_allowed
        elif current_allowed is None:
            effective = snapshot_allowed
        else:
            effective = tuple(
                name for name in snapshot_allowed if name in current_allowed
            )

        # 三方复核的第三面：上游还在不在这条链上。放在这里而不是路由层，
        # 是因为它和「B 还可用吗」是同一类判断，必须一起生效——
        # 分开写，早晚会出现只过了其中一道的路径。
        await self._review_delegation(approval, boundary_of(agent))

        return effective, snapshot_write and bool(agent.can_write)

    async def _load_boundary(self, hop: dict[str, Any]) -> AgentBoundary:
        """按链上的一跳取它**现在**的边界。

        名字只用于报错：复核必须问「还是当初那个 Agent 吗」，而名字在删除后
        可以被新 Agent 占用——按名字复核，等于让新建的同名 Agent 继承一张
        别人批过的写单。

        链上没有 id 的一跳（平台基线、没有选 Agent 的对话）直接跳过：
        它没有配置行，也就没有任何东西能收窄，无从复核也无需要复核。
        """
        raw_id = str(hop.get("id") or "")
        name = str(hop.get("name") or "")

        if not raw_id:
            return AgentBoundary()

        try:
            agent_id = uuid.UUID(raw_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise DelegationRevoked(
                "委派链上的智能体标识无效，本次不会执行。"
            ) from exc

        async with self._session_factory() as session:
            agent = await session.get(Agent, agent_id)

        if agent is None:
            raise DelegationRevoked(
                f"发起这次委派的智能体「{name or raw_id}」已被删除，本次不会执行。"
            )
        if agent.status != AgentStatus.ACTIVE:
            raise DelegationRevoked(
                f"发起这次委派的智能体「{agent.display_name or agent.name}」"
                "已停用，本次不会执行。"
            )

        return boundary_of(agent)

    async def _review_delegation(
        self,
        approval: ApprovalRequest,
        boundary: AgentBoundary,
    ) -> None:
        """逐跳复核：每一跳的**当前**边界必须仍然包含它下面那一跳。

        顺序是从直接上级往根走：`B ⊆ A`、`C ⊆ B`。这样一旦某一跳被收窄，
        报出来的就是离执行最近的那一处，而不是笼统的「委派链已失效」。

        只复核直接上级是不够的：A 收窄而 B 没变时，「A→B→C」里的 C 看起来
        完全合法，但它已经是一次不该发生的委派的产物的产物的产物。
        A2A 文档把这条写成「B 快照 ∩ B 当前 ∩ A 当前」，跳数大于 1 时
        就是这里的逐跳形式。

        非委派单（链为空）直接返回：行为与加这个功能之前逐字一致。
        """
        chain = approval.delegation_chain or []

        if not chain:
            return

        child = boundary

        for hop in reversed(chain):
            parent = await self._load_boundary(hop)
            ok, violation = agent_subset(child, parent)
            if not ok:
                parent_name = str(hop.get("name") or hop.get("id") or "上一级")
                raise DelegationRevoked(
                    f"发起这次委派的「{parent_name}」边界已收窄，"
                    f"{describe_violation(violation)}本次不会执行，请重新发起。"
                )
            child = parent

    async def confirm_and_execute(
        self,
        *,
        approval_id: uuid.UUID,
        context: ToolExecutionContext,
        confirmed_parameters: dict[str, Any],
    ) -> ApprovalExecutionResult:
        approval, claimed = await self._mark_executing(
            approval_id=approval_id,
            context=context,
            confirmed_parameters=confirmed_parameters,
        )

        # 已经执行过（或正在执行）的审批绝不再次触达外部系统。
        if approval.status is ApprovalStatus.VERIFIED:
            return self._already_done(approval, "该操作已完成，并通过回读核验。")

        if approval.status is ApprovalStatus.EXECUTED:
            return self._already_done(
                approval,
                "该操作此前已提交，本次不会重复执行。",
            )

        if not claimed:
            return ApprovalExecutionResult(
                approval_id=approval.id,
                status=ApprovalStatus.RECOVERY_REQUIRED,
                message=(
                    "该操作正在执行中或上次执行未正常结束，"
                    "系统不会自动重发，请先核查外部系统状态。"
                ),
                already_processed=True,
            )

        try:
            effective_tools, effective_write = await self._execution_agent_boundary(
                approval
            )
        except AgentUnavailable as exc:
            logger.warning(
                "智能体不可用，审批不再执行。",
                extra={
                    "event": "approved_agent_unavailable",
                    "approval_id": str(approval.id),
                    "agent_name": approval.agent_name,
                },
            )
            return await self._finish_rejected(
                approval=approval,
                error_code="AGENT_UNAVAILABLE",
                reason=str(exc),
            )
        except DelegationRevoked as exc:
            # 与 AGENT_UNAVAILABLE 分开记：B 可能完全正常，是把它叫来的那一跳
            # 变了。合成一个错误码，排障时会去查一个没有任何问题的 Agent。
            logger.warning(
                "委派链已失效，审批不再执行。",
                extra={
                    "event": "approved_delegation_revoked",
                    "approval_id": str(approval.id),
                    "delegation_chain": approval.delegation_chain,
                },
            )
            return await self._finish_rejected(
                approval=approval,
                error_code="DELEGATION_REVOKED",
                reason=str(exc),
            )

        if effective_tools is not None and approval.tool_name not in effective_tools:
            return await self._finish_rejected(
                approval=approval,
                error_code="AGENT_BOUNDARY_CHANGED",
                reason="该智能体的职责边界已调整，不再允许此操作，请重新发起。",
            )

        # 用 replace 而不是逐字段重建：这个上下文里有十几个字段，逐个抄写时
        # 每加一个新字段都要记得来这里补一行，漏了不会有任何报错——只会在
        # 某条很少走到的路径上悄悄退化。线上踩过：`is_platform_admin` 漏抄，
        # 结果是「对话里发起就停在审批、点确认却报当前身份已不具备授权」，
        # 而同一时刻同一个人点控制台按钮是能改的。
        exec_context = replace(
            context,
            approval_id=str(approval.id),
            idempotency_key=approval.idempotency_key,
            agent_id=approval.agent_id,
            agent_name=approval.agent_name or "",
            agent_revision=approval.agent_revision or 0,
            agent_allowed_tools=effective_tools,
            agent_can_write=effective_write,
        )

        try:
            tool_result = await self._tool_gateway.execute_approved(
                context=exec_context,
                tool_name=approval.tool_name,
                arguments=approval.parameters_json,
            )
        except (ToolPermissionDeniedError, ToolResourceDeniedError) as exc:
            # 授权拒绝是确定性失败：请求根本没有发往外部系统，
            # 因此绝不能标记为 EXECUTION_UNKNOWN（那会触发人工核查）。
            logger.warning(
                "已审批工具被授权拒绝。",
                extra={
                    "event": "approved_tool_denied",
                    "approval_id": str(approval.id),
                    "reason": type(exc).__name__,
                },
            )
            return await self._finish_rejected(
                approval=approval,
                error_code="NOT_AUTHORIZED",
                reason="当前身份已不具备该操作所需的授权。",
            )
        except ToolDisabledError:
            return await self._finish_rejected(
                approval=approval,
                error_code="TOOL_DISABLED",
                reason="该工具当前已停用。",
            )
        except Exception as exc:  # 兜底：Gateway 内部异常也应落到可恢复状态
            logger.exception(
                "已审批工具执行异常。",
                extra={"event": "approved_tool_exception", "approval_id": str(approval.id)},
            )
            return await self._finish_unknown(
                approval=approval,
                reason=f"执行异常：{type(exc).__name__}",
            )

        if tool_result.external_operation_id:
            await self._save_external_reference(
                approval_id=approval.id,
                external_operation_id=tool_result.external_operation_id,
            )

        if not tool_result.success:
            if tool_result.error_code in {"TOOL_TIMEOUT", "TOOL_HANDLER_EXCEPTION"}:
                return await self._finish_unknown(
                    approval=approval,
                    reason=f"外部调用结果未知（{tool_result.error_code}）。",
                )

            return await self._finish_failed(
                approval=approval,
                tool_result=tool_result,
            )

        return await self._finish_executed(
            approval=approval,
            tool_result=tool_result,
            context=exec_context,
        )

    # ---------------- 内部状态流转 ----------------

    def _already_done(
        self,
        approval: ApprovalRequest,
        message: str,
    ) -> ApprovalExecutionResult:
        return ApprovalExecutionResult(
            approval_id=approval.id,
            status=approval.status,
            message=message,
            already_processed=True,
        )

    async def _mark_executing(
        self,
        *,
        approval_id: uuid.UUID,
        context: ToolExecutionContext,
        confirmed_parameters: dict[str, Any],
    ) -> tuple[ApprovalRequest, bool]:
        """
        锁定并提交 EXECUTING，再执行外部工具。

        返回 (approval, claimed)：
        - claimed=True 表示本次调用成功占用了该审批，可以执行外部写操作；
        - claimed=False 表示审批已被处理或正在执行，调用方不得再次执行。

        进程崩溃时记录停在 EXECUTING，恢复任务会把它转入
        RECOVERY_REQUIRED，而不是重新执行。
        """
        now = datetime.now(UTC)
        expired = False
        expired_reason = ""

        async with self._session_factory() as session:
            async with session.begin():
                approval = await ApprovalRepository(session).get_for_update(
                    approval_id=approval_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                )

                if approval is None:
                    raise ApprovalNotFoundError("未找到审批记录。")

                if approval.status in {
                    ApprovalStatus.EXECUTED,
                    ApprovalStatus.VERIFIED,
                    ApprovalStatus.EXECUTING,
                    ApprovalStatus.RECOVERY_REQUIRED,
                    ApprovalStatus.EXECUTION_UNKNOWN,
                }:
                    return approval, False

                if approval.status is not ApprovalStatus.PENDING:
                    raise ApprovalStateError(
                        f"当前审批状态为 {approval.status.value}，不能执行。"
                    )

                if not is_actionable(approval, now=now):
                    approval.status = ApprovalStatus.EXPIRED
                    approval.completed_at = now
                    expired = True
                    expired_reason = "该操作预览已过期，请重新生成预览。"

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.APPROVAL_EXPIRED,
                        tenant_id=approval.tenant_id,
                        actor_id=approval.user_id,
                        resource_type="approval",
                        resource_id=str(approval.id),
                        thread_id=approval.thread_id,
                        run_id=approval.run_id,
                        success=False,
                        metadata={"tool_name": approval.tool_name},
                    )
                else:
                    if hash_parameters(confirmed_parameters) != approval.parameter_hash:
                        raise ApprovalParameterMismatchError(
                            "确认参数与预览内容不一致，本次不会执行。"
                        )

                    approval.status = ApprovalStatus.EXECUTING
                    approval.approved_at = now
                    approval.approved_by = context.user_id
                    approval.execution_started_at = now
                    approval.execution_attempts = (approval.execution_attempts or 0) + 1

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.APPROVAL_APPROVED,
                        tenant_id=approval.tenant_id,
                        actor_id=context.user_id,
                        resource_type="approval",
                        resource_id=str(approval.id),
                        thread_id=approval.thread_id,
                        run_id=approval.run_id,
                        metadata={
                            "tool_name": approval.tool_name,
                            "attempt": approval.execution_attempts,
                            "idempotency_key": approval.idempotency_key,
                        },
                    )

        # 事务已提交，过期状态不会因为抛异常而回滚。
        if expired:
            raise ApprovalExpiredError(expired_reason)

        return approval, True

    async def _save_external_reference(
        self,
        *,
        approval_id: uuid.UUID,
        external_operation_id: str,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                approval = await session.get(ApprovalRequest, approval_id)
                if approval is not None:
                    approval.external_operation_id = external_operation_id

    async def _finish_unknown(
        self,
        *,
        approval: ApprovalRequest,
        reason: str,
    ) -> ApprovalExecutionResult:
        now = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ApprovalRequest, approval.id)
                if row is not None:
                    row.status = ApprovalStatus.EXECUTION_UNKNOWN
                    row.completed_at = now
                    row.recovery_note = reason
                    row.error_code = "EXECUTION_UNKNOWN"

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.APPROVAL_EXECUTION_UNKNOWN,
                        tenant_id=row.tenant_id,
                        actor_id=row.user_id,
                        resource_type="approval",
                        resource_id=str(row.id),
                        thread_id=row.thread_id,
                        run_id=row.run_id,
                        success=False,
                        message=reason,
                        metadata={"tool_name": row.tool_name},
                    )

        await close_runs_for_approval(
            approval_id=str(approval.id),
            run_id=approval.run_id or None,
            tenant_id=approval.tenant_id,
            status=ApprovalStatus.EXECUTION_UNKNOWN,
        )

        return ApprovalExecutionResult(
            approval_id=approval.id,
            status=ApprovalStatus.EXECUTION_UNKNOWN,
            message=(
                "操作请求过程中出现异常，无法确认外部系统是否已受理。"
                "系统不会自动重试，请后续核查执行结果。"
            ),
        )

    async def _finish_rejected(
        self,
        *,
        approval: ApprovalRequest,
        error_code: str,
        reason: str,
    ) -> ApprovalExecutionResult:
        """
        授权/可用性拒绝：请求从未发往外部系统，结果是确定的"未执行"。

        与 EXECUTION_UNKNOWN 严格区分——后者需要人工核查外部系统，
        这里不需要，直接 FAILED 并把原因写进审计。
        """
        now = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ApprovalRequest, approval.id)
                if row is not None:
                    row.status = ApprovalStatus.FAILED
                    row.completed_at = now
                    row.error_code = error_code
                    row.error_message = reason
                    row.execution_result = {
                        "success": False,
                        "executed": False,
                        "error_code": error_code,
                    }

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.APPROVAL_FAILED,
                        tenant_id=row.tenant_id,
                        actor_id=row.user_id,
                        resource_type="approval",
                        resource_id=str(row.id),
                        thread_id=row.thread_id,
                        run_id=row.run_id,
                        success=False,
                        message=reason,
                        metadata={
                            "tool_name": row.tool_name,
                            "error_code": error_code,
                            "executed": False,
                        },
                    )

        await close_runs_for_approval(
            approval_id=str(approval.id),
            run_id=approval.run_id or None,
            tenant_id=approval.tenant_id,
            status=ApprovalStatus.FAILED,
            error_category=error_code,
        )

        return ApprovalExecutionResult(
            approval_id=approval.id,
            status=ApprovalStatus.FAILED,
            message=f"操作未执行：{reason}",
            error_code=error_code,
        )

    async def _finish_failed(
        self,
        *,
        approval: ApprovalRequest,
        tool_result: ToolExecutionResult,
    ) -> ApprovalExecutionResult:
        now = datetime.now(UTC)

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ApprovalRequest, approval.id)
                if row is not None:
                    row.status = ApprovalStatus.FAILED
                    row.completed_at = now
                    row.error_code = tool_result.error_code
                    row.error_message = tool_result.message
                    row.execution_result = {
                        "success": False,
                        "error_code": tool_result.error_code,
                        # 失败也可能带回有价值的输出（典型是代码的 stderr）。
                        # 只存 error_code 会让「没执行」和「执行了但报错」无法区分。
                        "message": tool_result.message,
                        "result": tool_result.result or {},
                    }

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.APPROVAL_FAILED,
                        tenant_id=row.tenant_id,
                        actor_id=row.user_id,
                        resource_type="approval",
                        resource_id=str(row.id),
                        thread_id=row.thread_id,
                        run_id=row.run_id,
                        success=False,
                        message=tool_result.message,
                        metadata={
                            "tool_name": row.tool_name,
                            "error_code": tool_result.error_code,
                        },
                    )

        await close_runs_for_approval(
            approval_id=str(approval.id),
            run_id=approval.run_id or None,
            tenant_id=approval.tenant_id,
            status=ApprovalStatus.FAILED,
            error_category=tool_result.error_code,
        )

        return ApprovalExecutionResult(
            approval_id=approval.id,
            status=ApprovalStatus.FAILED,
            message=(
                tool_result.message
                or "操作未确认完成。外部系统返回失败，不会将其表述为已完成。"
            ),
            tool_result=tool_result,
        )

    async def _finish_executed(
        self,
        *,
        approval: ApprovalRequest,
        tool_result: ToolExecutionResult,
        context: ToolExecutionContext,
    ) -> ApprovalExecutionResult:
        verify_context = ToolExecutionContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
            request_id=context.request_id,
            roles=context.roles,
            scopes=context.scopes,
            approval_id=context.approval_id,
            idempotency_key=approval.idempotency_key,
            external_operation_id=tool_result.external_operation_id,
        )

        verification = await self._tool_gateway.verify(
            context=verify_context,
            tool_name=approval.tool_name,
            arguments=approval.parameters_json,
        )

        verified = bool(verification and verification.verified)
        now = datetime.now(UTC)

        status = ApprovalStatus.VERIFIED if verified else ApprovalStatus.EXECUTED

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ApprovalRequest, approval.id)
                if row is not None:
                    row.status = status
                    row.executed_at = now
                    row.completed_at = now
                    if verified:
                        row.verified_at = now
                    row.execution_result = {
                        "success": tool_result.success,
                        "message": tool_result.message,
                        "result": tool_result.result,
                    }
                    row.verification = (
                        verification.verification if verification else None
                    )

                    await write_audit_event(
                        session,
                        event_type=(
                            AuditEventType.APPROVAL_VERIFIED
                            if verified
                            else AuditEventType.APPROVAL_EXECUTED
                        ),
                        tenant_id=row.tenant_id,
                        actor_id=row.user_id,
                        resource_type="approval",
                        resource_id=str(row.id),
                        thread_id=row.thread_id,
                        run_id=row.run_id,
                        success=True,
                        metadata={
                            "tool_name": row.tool_name,
                            "verified": verified,
                            "external_operation_id": row.external_operation_id,
                        },
                    )

        await close_runs_for_approval(
            approval_id=str(approval.id),
            run_id=approval.run_id or None,
            tenant_id=approval.tenant_id,
            status=status,
        )

        message = (
            "操作已执行，并已通过外部回读核验。"
            if verified
            else "操作请求已提交，但尚未获得可确认的回读结果。"
        )

        return ApprovalExecutionResult(
            approval_id=approval.id,
            status=status,
            message=message,
            tool_result=tool_result,
        )
