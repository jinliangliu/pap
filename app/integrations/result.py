from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ExternalCallResult(BaseModel):
    ok: bool
    business_status: str
    http_status: int | None = None

    data: dict[str, Any] | list[Any] | None = None

    external_operation_id: str | None = None

    retryable: bool = False
    reason: str | None = None
