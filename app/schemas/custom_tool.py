# app/schemas/custom_tool.py
"""自定义 HTTP Tool 的管理端请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.models.approval import RiskLevel
from app.schemas.common import APIModel, as_utc

ParamLocation = Literal["path", "query", "body", "header"]
ParamType = Literal["string", "integer", "number", "boolean"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# 这些头由 HTTP 客户端自己管，或会把凭据写进配置表，一律不允许。
_FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "authorization",
        "cookie",
        "proxy-authorization",
    }
)


class ToolParamIn(APIModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    location: ParamLocation = "query"
    type: ParamType = "string"
    required: bool = True
    description: str = Field(default="", max_length=512)
    enum: list[str] = Field(default_factory=list, max_length=32)


def _check_headers(value: dict[str, str]) -> dict[str, str]:
    for key, item in value.items():
        lowered = key.strip().lower()
        if not lowered:
            raise ValueError("请求头名称不能为空。")
        if lowered in _FORBIDDEN_HEADERS:
            raise ValueError(
                f"不允许配置 {key}：鉴权类请求头请改用租户凭据（credential_ref），"
                "其余由客户端自行管理。"
            )
        if len(item) > 1024:
            raise ValueError(f"请求头 {key} 的值过长。")
    return {key.strip(): item.strip() for key, item in value.items()}


class CustomToolCreate(APIModel):
    name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
        description="工具标识，会进模型目录与审计，保存后不可更改。",
    )
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)

    method: HttpMethod = "GET"
    url: str = Field(min_length=1, max_length=1024)

    risk_level: RiskLevel = RiskLevel.READ
    timeout_seconds: int = Field(default=20, ge=1, le=120)

    params: list[ToolParamIn] = Field(default_factory=list, max_length=32)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_headers(value)


class CustomToolUpdate(APIModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    method: HttpMethod | None = None
    url: str | None = Field(default=None, min_length=1, max_length=1024)
    risk_level: RiskLevel | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    params: list[ToolParamIn] | None = Field(default=None, max_length=32)
    headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_headers(value)


class CustomToolResponse(APIModel):
    id: int
    name: str
    display_name: str
    description: str

    method: str
    url: str
    risk_level: RiskLevel
    timeout_seconds: int

    params: list[ToolParamIn] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool

    # 由风险等级推导，前端不必自己猜「这个工具会不会要审批」。
    requires_approval: bool = False
    # 当前进程的网关注册状态：保存成功但注册失败时两者会不一致。
    registered: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)

    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value)


class CustomToolListResponse(APIModel):
    items: list[CustomToolResponse] = Field(default_factory=list)
    total: int = 0
    # 网关里当前实际注册的数量，用来对照「库里 5 条但只注册了 3 条」。
    registered_total: int = 0


class CustomToolImportRequest(APIModel):
    """导入的是一份可迁移的工具定义（导出文件的 tools 段）。"""

    tools: list[CustomToolCreate] = Field(default_factory=list, max_length=200)
    mode: Literal["merge", "overwrite"] = Field(
        default="merge",
        description="merge 只新增缺失项；overwrite 同时覆盖同名工具。",
    )


class CustomToolImportItem(APIModel):
    name: str
    action: Literal["created", "updated", "skipped"]
    message: str = ""


class CustomToolImportResponse(APIModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    items: list[CustomToolImportItem] = Field(default_factory=list)


class CustomToolExportResponse(APIModel):
    version: int = 1
    exported_at: datetime
    tools: list[CustomToolCreate] = Field(default_factory=list)


class ParseUrlRequest(APIModel):
    url: str = Field(min_length=1, max_length=1024)


class ParseUrlResponse(APIModel):
    """从 URL 反解出的 path 参数；只解析，不保存。"""

    url: str
    params: list[ToolParamIn] = Field(default_factory=list)


class ParseCurlRequest(APIModel):
    curl: str = Field(min_length=1, max_length=8000)


class ParseCurlResponse(APIModel):
    method: str
    url: str
    params: list[ToolParamIn] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    # 被安全策略拦下的请求头（Authorization / Cookie），前端要明确告知。
    dropped_headers: list[str] = Field(default_factory=list)
    has_body: bool = False
