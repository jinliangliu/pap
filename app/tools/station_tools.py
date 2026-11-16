# app/tools/station_tools.py
"""场站只读工具。

真实模式下通过统一 HTTP Adapter 调用上游平台；
未配置 station_platform_base_url 时进入显式 mock 模式，
返回值带 source="mock"，避免被误当作真实外部数据。

站点授权由 Gateway 在调用前校验，handler 不重复实现权限逻辑。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
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

    from app.integrations.http_client import PlatformHttpClient

    return StationPlatformAdapter(PlatformHttpClient())


async def get_station_snapshot(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    station_id = str(arguments["station_id"])
    adapter = _adapter()

    if adapter is None:
        return ToolExecutionResult(
            success=True,
            verified=False,
            result={
                "station_id": station_id,
                "source": "mock",
                "pressure_mpa": 0.32,
                "sampled_at": "2026-11-02T10:30:00+08:00",
            },
            message=(
                "当前未配置场站平台地址，返回的是演示数据，"
                "不代表真实场站状态。"
            ),
            verification={
                "verified": False,
                "reason": "mock_mode",
            },
        )

    external = await adapter.get_station_snapshot(
        station_id=station_id,
        credential=context.credential,
    )

    if not external.get("ok"):
        return ToolExecutionResult(
            success=False,
            result={},
            message="外部场站平台暂时不可用，未能取得实时数据。",
            error_code="STATION_PLATFORM_UNAVAILABLE",
            retryable=bool(external.get("retryable")),
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "station_id": station_id,
            "source": "platform",
            "data": external.get("data"),
        },
        message="已从场站平台读取授权场站的实时数据。",
        verification={"verified": True, "http_status": external.get("http_status")},
    )


def build_station_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_station_snapshot",
            display_name="查询场站快照",
            description="读取已授权场站的压力与采样时间。",
            kind="http",
            risk_level=RiskLevel.READ,
            required_scopes=("station.read",),
            station_id_argument="station_id",
            input_schema={
                "type": "object",
                "properties": {
                    "station_id": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["station_id"],
                "additionalProperties": False,
            },
            handler=get_station_snapshot,
            verification_hint="读取结果为平台实时返回值。",
        )
    ]
