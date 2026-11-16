# app/services/audit_service.py
"""审计写入。

原则：记录发生了什么，不存敏感明文。
- 不写 Token / Cookie / Authorization / 密码 / 私钥；
- 不写完整模型输入输出；
- metadata_json 只放脱敏后的摘要。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.audit import AuditEventType, AuditLog
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)


def _coerce_event_type(event_type: AuditEventType | str) -> AuditEventType:
    if isinstance(event_type, AuditEventType):
        return event_type

    try:
        return AuditEventType(event_type)
    except ValueError:
        return AuditEventType.SECURITY_EVENT


async def write_audit_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType | str,
    tenant_id: str,
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    success: bool = True,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        event_type=_coerce_event_type(event_type),
        resource_type=resource_type,
        resource_id=resource_id,
        thread_id=thread_id,
        run_id=run_id,
        request_id=request_id,
        success=success,
        message=message,
        metadata_json=metadata or {},
    )

    session.add(row)
    await session.flush()
    return row


async def write_admin_action(
    session: AsyncSession,
    *,
    actor_id: str | None,
    actor_subject: str | None,
    actor_tenant: str | None,
    target_tenant: str,
    operation: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """平台管理动作的统一写法。

    管理通道的每一条写操作都必须经过这里：谁（subject）、从哪个租户、
    对哪个目标租户做了什么。缺少这层元数据，审计表就只能看到一串 id。
    """
    return await write_audit_event(
        session,
        event_type=AuditEventType.ADMIN_ACTION,
        tenant_id=target_tenant,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        success=True,
        message=operation,
        metadata={
            "operation": operation,
            "actor_subject": actor_subject,
            "actor_tenant": actor_tenant,
            "target_tenant": target_tenant,
            **(metadata or {}),
        },
    )


class DbAuditSink:
    """Gateway 审计落库适配器。审计失败不影响业务结论。"""

    async def record(self, event: str, payload: dict[str, Any]) -> None:
        mapping = {
            "tool_executed": AuditEventType.TOOL_SUCCEEDED,
            "tool_denied": AuditEventType.TOOL_DENIED,
            "tool_requested": AuditEventType.TOOL_REQUESTED,
        }

        event_type = mapping.get(event, AuditEventType.TOOL_REQUESTED)

        # 拒绝本身就是「没成功」，但**不能**因此改写成 tool_failed：
        # 「被拒绝」和「跑了但失败」是两件事，前者是治理信号、后者是故障信号，
        # 归成一类之后审计里就再也数不出「这个 Agent 边界被撞了几次」。
        if payload.get("success") is False and event_type is not AuditEventType.TOOL_DENIED:
            event_type = AuditEventType.TOOL_FAILED

        metadata = {
            key: payload.get(key)
            for key in (
                "tool_name",
                "risk_level",
                "success",
                "verified",
                "error_code",
                "duration_ms",
                "argument_keys",
                "approval_id",
                # 拒绝路径专用：谁拒的 + 当时是哪个智能体在跑。
                "denial_reason",
                "agent_name",
                "agent_revision",
            )
        }

        # 审计页唯一的人话列是 message，而工具事件原先一律留空（显示「—」）。
        # 这意味着「谁调用了什么」只能点开 metadata 才看得出来——构建助手的
        # 查询会话在对话台里看不到，这里就是它唯一的痕迹，至少得能读出一句。
        # 参数值仍然不记：argument_keys 只有键名。
        message = payload.get("message")
        if message is None and payload.get("agent_name"):
            verb = "尝试调用" if event_type is AuditEventType.TOOL_DENIED else "调用"
            message = f"{payload['agent_name']} {verb} {payload.get('tool_name') or '工具'}"

        tenant_id = str(payload.get("tenant_id") or "").strip()
        if not tenant_id:
            # 没有租户就写不进 audit_logs：RLS 要求事务里先设 app.tenant_id。
            # 硬写只会换来一条 InsufficientPrivilege，还被下面的 except 吞掉，
            # 看起来「写了」其实没有——不如在这里明说。
            logger.warning(
                "工具审计缺少租户标识，已跳过写入。",
                extra={"event": "audit_write_no_tenant", "tool_name": payload.get("tool_name")},
            )
            return

        # 这里必须先进入租户上下文再开会话：RLS 的过滤条件取自事务级的
        # `app.tenant_id`，而新会话没有继承调用方的 contextvar。
        # 少了这一句，网关的所有审计（成功、失败、拒绝）都会被
        # 「new row violates row-level security policy」顶掉，异常还被吞成日志——
        # 现象是审计页上一条工具记录都没有，而所有人都以为记了。
        try:
            with tenant_scope(tenant_id):
                async with async_session_factory() as session:
                    async with session.begin():
                        await write_audit_event(
                            session,
                            event_type=event_type,
                            tenant_id=tenant_id,
                            actor_id=payload.get("user_id"),
                            resource_type="tool",
                            resource_id=payload.get("tool_name"),
                            thread_id=payload.get("thread_id"),
                            run_id=payload.get("run_id"),
                            success=bool(payload.get("success", True)),
                            message=message,
                            metadata=metadata,
                        )
        except Exception:
            logger.exception("工具审计写入失败。", extra={"event": "audit_write_failed"})
