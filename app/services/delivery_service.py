# app/services/delivery_service.py
"""定时任务的投递：授权校验 → 当前身份解析 → 工具网关 → 独立审计。

沙箱不联网、不持凭据（docs/TRANSACTION.md §1），所以它只产出草稿。真正
出网的是这里，走平台侧已注册的工具，经过网关原有的 scope / 租户策略 /
站点授权校验——**授权范围只做收窄，不替代这些校验**。

两条容易被踩坏的边界：

1. **不继承管理员豁免**。`is_platform_admin=False` 写死：定时任务无人值守，
   一次授权不该把「管理员跳过 scope」变成常驻能力。
2. **不快照 scopes/roles**。每次投递重新从 RBAC 解析用户当前权限，撤权后
   下一次触发立即被网关拒绝。grant 只管「哪个工具、什么参数」。

投递结果与任务终态解耦：计算产物成功落库后，无论投递成功/拒绝/未知，
任务都是 SUCCEEDED，结论写在 `output.delivery` 与审计里。这是规划里的
「投递失败不影响计算产物」——但绝不把失败伪装成已送达。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.audit import AuditEventType
from app.models.task import Schedule, Task, TaskStatus
from app.services.audit_service import write_audit_event
from app.services.authorization_service import get_user_permissions, get_user_roles
from app.services.delivery_grant_service import (
    DeliveryGrantError,
    build_grant,
    check_delivery,
)
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """写入 `task.output_json["delivery"]` 的结论。字段刻意都是可 JSON 化的。"""

    status: str
    tool_name: str
    error_code: str | None = None
    message: str = ""
    external_operation_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool_name": self.tool_name,
            "error_code": self.error_code,
            "message": self.message,
            "external_operation_id": self.external_operation_id,
        }


async def _current_permissions(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """每次都重新解析，不用授权时快照的权限（收窄必须立即生效）。"""
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("任务所属用户标识不是合法 UUID，无法解析权限。") from None

    roles = await get_user_roles(session, user_id=parsed, tenant_id=tenant_id)
    scopes = await get_user_permissions(session, user_id=parsed, tenant_id=tenant_id)
    return tuple(roles), tuple(scopes)


async def _write_audit(
    *,
    tenant_id: str,
    user_id: str,
    task_id: uuid.UUID,
    event_type: AuditEventType,
    outcome: DeliveryOutcome,
) -> None:
    # 单开会话：与任务终态事务分开，投递审计不能因为任务那边的回滚消失。
    async with async_session_factory() as session:
        async with session.begin():
            await write_audit_event(
                session,
                event_type=event_type,
                tenant_id=tenant_id,
                actor_id=user_id,
                resource_type="schedule_delivery",
                resource_id=str(task_id),
                thread_id="",
                run_id="",
                success=outcome.status == "delivered",
                message=outcome.message or None,
                metadata={
                    "tool_name": outcome.tool_name,
                    "status": outcome.status,
                    "error_code": outcome.error_code,
                    "external_operation_id": outcome.external_operation_id,
                },
            )


async def authorize_delivery(
    session: AsyncSession,
    *,
    schedule_id: uuid.UUID,
    tool_name: str,
    parameter_bounds: dict[str, Any],
    created_by: str,
    expires_at: datetime | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """为一条定时任务授权投递。调用方须在用户请求里、租户上下文已绑定。

    这是「一次授权」的落点：之后每条任务在触发时快照这份 grant，不必每天
    等人点确认。授权只收窄工具与参数；权限仍来自每次投递时的 RBAC 解析。
    HTTP/UI 配置入口留 P2（与 schedule 本身的配置入口一起做）。
    """
    grant = build_grant(
        tool_name=tool_name,
        parameter_bounds=parameter_bounds,
        created_by=created_by,
        expires_at=expires_at,
        now=now,
    )
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise DeliveryGrantError("定时任务不存在，无法授权投递。")
    if created_by != row.user_id:
        # 授权是「我允许我的定时任务替我发这些」，不能由第三人代授。
        # 平台管理员若需代管，走单独的治理入口，不混进这条链路。
        raise DeliveryGrantError("只有定时任务所属用户可以授权其投递。")

    row.delivery_grant_json = grant
    row.updated_at = now or datetime.now(UTC)
    await write_audit_event(
        session,
        event_type=AuditEventType.APPROVAL_CREATED,
        tenant_id=row.tenant_id,
        actor_id=created_by,
        resource_type="schedule_delivery",
        resource_id=str(schedule_id),
        metadata={
            "tool_name": grant["tool_name"],
            "expires_at": grant["expires_at"],
            # 只记白名单的键名，参数值可能是收件人，不进审计。
            "parameter_keys": sorted(grant["parameter_bounds"]),
        },
    )
    return grant


async def revoke_delivery(
    session: AsyncSession,
    *,
    schedule_id: uuid.UUID,
    actor_id: str,
    now: datetime | None = None,
) -> bool:
    """撤销授权，并即时打断该 schedule 下所有在途任务的投递。

    级联改的是任务手里的授权快照，不是任务状态：计算（沙箱出草稿）不需要
    外部授权，撤销只切断出网。已 SUCCEEDED 的任务不回改——投递已经发生，
    撤销收不回来，改状态只会让记录说谎。

    边界：已经在飞行中的那一次网关请求无法撤回；这里挡住的是「还没开始投递」
    的任务。这也是不引入 MQ / 跨请求取消令牌的理由（见 docs/TRANSACTION.md §7）。
    """
    row = await session.get(Schedule, schedule_id)
    if row is None or not isinstance(row.delivery_grant_json, dict):
        return False

    moment = now or datetime.now(UTC)
    revoked_at = moment.isoformat()

    # JSON 列不跟踪原地修改：必须整体重新赋值，否则撤销写不进库。
    revoked = dict(row.delivery_grant_json)
    revoked["revoked_at"] = revoked_at
    row.delivery_grant_json = revoked
    row.updated_at = moment
    await write_audit_event(
        session,
        event_type=AuditEventType.APPROVAL_REJECTED,
        tenant_id=row.tenant_id,
        actor_id=actor_id,
        resource_type="schedule_delivery",
        resource_id=str(schedule_id),
        message="定时任务投递授权已撤销。",
        metadata={"tool_name": revoked.get("tool_name", "")},
    )

    # 按 schedule_id 精确反查，不解析 idempotency_key 前缀。
    in_flight = await session.scalars(
        select(Task).where(
            Task.schedule_id == schedule_id,
            Task.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING)),
        )
    )
    for task in in_flight:
        grant = task.delivery_grant_json
        if not isinstance(grant, dict) or grant.get("revoked_at") is not None:
            continue
        interrupted = dict(grant)
        interrupted["revoked_at"] = revoked_at
        task.delivery_grant_json = interrupted  # 整体赋值，同上
        task.updated_at = moment
        await write_audit_event(
            session,
            event_type=AuditEventType.APPROVAL_REJECTED,
            tenant_id=task.tenant_id,
            actor_id=actor_id,
            resource_type="schedule_delivery",
            resource_id=str(task.id),
            message="投递授权已撤销，在途任务的投递被阻断；计算产物不受影响。",
            metadata={
                "tool_name": interrupted.get("tool_name", ""),
                "schedule_id": str(schedule_id),
            },
        )

    return True


async def _settle(
    *,
    tenant_id: str,
    user_id: str,
    task_id: uuid.UUID,
    outcome: DeliveryOutcome,
) -> DeliveryOutcome:
    """把投递结论写进独立审计。状态到事件的映射只在这里定义一次。"""
    event_type = {
        "delivered": AuditEventType.APPROVAL_EXECUTED,
        "failed": AuditEventType.APPROVAL_FAILED,
        "denied": AuditEventType.APPROVAL_FAILED,
        "unknown": AuditEventType.APPROVAL_EXECUTION_UNKNOWN,
    }[outcome.status]
    await _write_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
        event_type=event_type,
        outcome=outcome,
    )
    return outcome


async def deliver_task_output(
    *,
    task_id: uuid.UUID,
    gateway: Any | None = None,
) -> DeliveryOutcome:
    """执行一次定时投递。不抛投递侧异常；只把结论写进返回值与审计。

    调用方在任务终态落库**之后**调用——投递失败不能把已产出的制品抹掉。
    `gateway` 可注入供测试；缺省取全局网关（延迟导入避免 builtins 注册环）。

    这里自己读 task，而不是接收调用方手里那个对象：SQLAlchemy 默认
    `expire_on_commit`，跨会话传 ORM 实例会在读属性时抛 DetachedInstanceError，
    那会让「投递成功、记账失败」变成一种很难复现的假故障。
    """
    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return DeliveryOutcome(
                status="denied",
                tool_name="",
                error_code="DELIVERY_TASK_MISSING",
                message="任务不存在，未执行投递。",
            )

        tenant_id = task.tenant_id
        user_id = task.user_id
        raw_delivery = (task.input_json or {}).get("delivery")
        grant = task.delivery_grant_json

        # 当前身份：不快照、不提权，撤权后下一次触发立即生效。
        try:
            roles, scopes = await _current_permissions(
                session, tenant_id=tenant_id, user_id=user_id
            )
        except ValueError as exc:
            return await _settle(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
                outcome=DeliveryOutcome(
                    status="denied",
                    tool_name="",
                    error_code="DELIVERY_IDENTITY_INVALID",
                    message=str(exc),
                ),
            )

    async def settle(outcome: DeliveryOutcome) -> DeliveryOutcome:
        return await _settle(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            outcome=outcome,
        )

    if not isinstance(raw_delivery, dict):
        return await settle(
            DeliveryOutcome(
                status="denied",
                tool_name="",
                error_code="DELIVERY_NOT_CONFIGURED",
                message="任务没有配置投递内容。",
            )
        )

    tool_name = str(raw_delivery.get("tool_name") or "").strip()
    arguments = raw_delivery.get("arguments")
    if arguments is None:
        arguments = {}

    # 1) 授权范围：closed set。越界是确定性拒绝，请求从未出网。
    try:
        check_delivery(grant, tool_name=tool_name, arguments=arguments)
    except DeliveryGrantError as exc:
        return await settle(
            DeliveryOutcome(
                status="denied",
                tool_name=tool_name,
                error_code="DELIVERY_NOT_AUTHORIZED",
                message=str(exc),
            )
        )

    context = ToolExecutionContext(
        tenant_id=tenant_id,
        user_id=user_id,
        thread_id=f"schedule:{task_id}",
        run_id=f"task:{task_id}",
        request_id="",
        roles=roles,
        scopes=scopes,
        # 定时任务不继承管理员豁免，也不带任何智能体边界快照：
        # 网关按平台基线 + 用户当前权限判定。
        is_platform_admin=False,
        idempotency_key=f"delivery:{task_id}",
    )

    # 2) 投递。网关原有的 scope/租户策略/站点校验照常生效；
    #    execute_approved 只是「已获授权」的执行入口，不是绕过校验的后门。
    if gateway is None:
        from app.tools.dependencies import get_tool_gateway

        gateway = get_tool_gateway()

    try:
        result = await gateway.execute_approved(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
        )
    except Exception as exc:
        # 网关异常统一按「结果未知」处理：不重试、不声称失败，交人工核查。
        # 权限类异常是确定性拒绝，但网关把它们包在 ToolGatewayError 家族里，
        # 这里不逐个猜类型——宁可标未知让人看一眼，也不要误报「没发出去」。
        logger.warning(
            "定时投递结果未知。",
            extra={
                "event": "schedule_delivery_unknown",
                "task_id": str(task_id),
                "tool_name": tool_name,
                "error": type(exc).__name__,
            },
        )
        return await settle(
            DeliveryOutcome(
                status="unknown",
                tool_name=tool_name,
                error_code="DELIVERY_UNKNOWN",
                message=f"投递结果未知：{type(exc).__name__}",
            )
        )

    if not result.success:
        return await settle(
            DeliveryOutcome(
                status="failed",
                tool_name=tool_name,
                error_code=result.error_code or "DELIVERY_FAILED",
                message=result.message or "投递未成功。",
            )
        )

    return await settle(
        DeliveryOutcome(
            status="delivered",
            tool_name=tool_name,
            message=result.message or "已投递。",
            external_operation_id=result.external_operation_id,
        )
    )
