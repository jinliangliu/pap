from __future__ import annotations

from typing import Any

from app.integrations.http_client import PlatformHttpClient

# 租户专属凭据的透传头。平台只做转发，不在日志/审计里记录该值。
CREDENTIAL_HEADER = "X-Tenant-Credential"


def _credential_headers(credential: str | None) -> dict[str, str] | None:
    if not credential:
        return None

    return {CREDENTIAL_HEADER: credential}


class StationPlatformAdapter:
    def __init__(self, client: PlatformHttpClient) -> None:
        self.client = client

    async def get_station_snapshot(
        self,
        *,
        station_id: str,
        credential: str | None = None,
    ) -> dict[str, Any]:
        result = await self.client.get(
            path="/api/v1/stations/snapshot",
            params={"station_id": station_id},
            headers=_credential_headers(credential),
        )
        return result.model_dump()

    async def submit_control(
        self,
        *,
        station_id: str,
        command: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        result = await self.client.post_write(
            path="/api/v1/control/commands",
            json_body={
                "station_id": station_id,
                "command": command,
            },
            idempotency_key=idempotency_key,
        )
        return result.model_dump()

    async def query_control_by_idempotency_key(
        self,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        result = await self.client.get(
            path="/api/v1/control/commands/by-idempotency-key",
            params={"idempotency_key": idempotency_key},
        )
        return result.model_dump()
