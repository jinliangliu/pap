# app/tools/mock_write_tools.py
"""带副作用的写工具示例：创建维护工单。

真实实现必须：
- 把 context.idempotency_key 透传给外部服务；
- 保存外部 operation/ticket ID；
- 超时或网络中断时不能假定未写入；
- 通过回读核验后才能对用户表述为“已完成”。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.config import settings
from app.integrations.http_client import get_platform_client
from app.integrations.station_platform import StationPlatformAdapter
from app.models.approval import RiskLevel
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)


def _adapter() -> StationPlatformAdapter | None:
    if not settings.station_platform_base_url:
        return None

    return StationPlatformAdapter(get_platform_client())


async def create_maintenance_ticket(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    adapter = _adapter()
    idempotency_key = context.idempotency_key or ""

    if adapter is None:
        # 显式演示模式：不触达外部系统，但保留幂等键语义。
        operation_id = (
            "demo-ticket-"
            + str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key or "no-key"))
        )
        return ToolExecutionResult(
            success=True,
            verified=False,
            external_operation_id=operation_id,
            result={
                "ticket_id": operation_id,
                "station_id": arguments["station_id"],
                "status": "ACCEPTED",
                "source": "mock",
            },
            message="工单已在演示模式下受理，尚未核验。",
            verification={"verified": False, "reason": "mock_mode"},
        )

    external = await adapter.submit_control(
        station_id=str(arguments["station_id"]),
        command="create_maintenance_ticket",
        idempotency_key=idempotency_key,
    )

    if not external.get("ok"):
        return ToolExecutionResult(
            success=False,
            result={},
            message="外部系统未确认受理该工单，不能视为已完成。",
            error_code=str(external.get("business_status", "EXTERNAL_WRITE_FAILED")),
            retryable=False,
        )

    return ToolExecutionResult(
        success=True,
        verified=False,
        external_operation_id=external.get("external_operation_id"),
        result=external.get("data") or {},
        message="工单已提交，等待回读核验。",
        verification={"verified": False, "reason": "awaiting_readback"},
    )


async def verify_maintenance_ticket(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    adapter = _adapter()

    if adapter is None:
        if not context.external_operation_id:
            return ToolExecutionResult(
                success=False,
                message="缺少外部工单标识，无法核验。",
                error_code="MISSING_EXTERNAL_OPERATION_ID",
            )

        return ToolExecutionResult(
            success=True,
            verified=True,
            external_operation_id=context.external_operation_id,
            result={
                "ticket_id": context.external_operation_id,
                "status": "CREATED",
                "source": "mock",
            },
            message="演示模式下已确认工单存在。",
            verification={"verified": True, "reason": "mock_readback"},
        )

    external = await adapter.query_control_by_idempotency_key(
        idempotency_key=context.idempotency_key or "",
    )

    if not external.get("ok"):
        return ToolExecutionResult(
            success=False,
            message="回读外部系统失败，当前无法确认工单状态。",
            error_code="READBACK_UNAVAILABLE",
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        external_operation_id=external.get("external_operation_id"),
        result=external.get("data") or {},
        message="已通过外部回读确认工单状态。",
        verification={"verified": True, "http_status": external.get("http_status")},
    )


def build_write_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="create_maintenance_ticket",
            display_name="创建维护工单",
            description="为已授权场站创建维护工单，属于外部写入操作。",
            kind="http",
            risk_level=RiskLevel.WRITE,
            requires_approval=True,
            required_scopes=("maintenance.ticket.create",),
            station_id_argument="station_id",
            input_schema={
                "type": "object",
                "properties": {
                    "station_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                    },
                },
                "required": ["station_id", "title", "description"],
                "additionalProperties": False,
            },
            handler=create_maintenance_ticket,
            verifier=verify_maintenance_ticket,
            verification_hint="通过外部系统按幂等键回读工单状态。",
        )
    ]
