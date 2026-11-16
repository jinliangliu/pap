# app/schemas/common.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def as_utc(value: datetime | None) -> datetime | None:
    """把「无时区但其实是 UTC」的时间补上 tzinfo。

    PostgreSQL timestamptz 读出来是 aware 的；SQLite 的 DateTime(timezone=True)
    只是约定，读出来是 naive。不补这一步，前端会按本地时区解释，
    出现「刚创建的工具显示 8 小时前」这类明显错误。
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class APIModel(BaseModel):
    """项目内 API Schema 的统一基类。"""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class MessageResponse(APIModel):
    """简单消息响应。"""

    message: str


class ErrorDetail(APIModel):
    """统一错误详情。"""

    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(APIModel):
    """统一错误响应。"""

    error: ErrorDetail
    request_id: str | None = None


class PaginationParams(APIModel):
    """分页查询参数。"""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class PageMeta(APIModel):
    """分页元信息。"""

    offset: int
    limit: int
    total: int


class PageResponse(APIModel, Generic[T]):
    """统一分页响应。"""

    items: list[T]
    page: PageMeta


class HealthResponse(APIModel):
    """健康检查响应。"""

    status: str = Field(
        description="服务整体状态，如 ok、degraded、unavailable。",
    )
    app_name: str
    version: str
    environment: str
    timestamp: datetime
    checks: dict[str, bool] = Field(default_factory=dict)


class ResourceRef(APIModel):
    """可审计资源的轻量引用。"""

    id: uuid.UUID | str
    resource_type: str
