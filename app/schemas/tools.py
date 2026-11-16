# app/schemas/tools.py
from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.approval import RiskLevel
from app.schemas.common import APIModel


class ToolInfoResponse(APIModel):
    """供管理端或前端展示的脱敏工具信息。"""

    name: str
    display_name: str
    description: str

    kind: str
    risk_level: RiskLevel

    # builtin / mcp / custom：详情面板据此决定能不能编辑、能不能导出。
    source: str = "builtin"
    timeout_seconds: int = 20

    enabled: bool
    requires_approval: bool

    required_scopes: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)

    input_schema: dict[str, Any]
    verification_hint: str | None = None


class ToolCallRequest(APIModel):
    """
    供 Agent 内部服务或受控 API 使用的工具调用请求。

    不建议把此接口作为“让外部客户端任意调用工具”的公共入口。
    普通用户应通过聊天请求驱动 Agent，再由 Agent 规划工具调用。
    """

    tool_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(
        default="用户请求需要。",
        min_length=1,
        max_length=1000,
    )


class ToolCallPlanResponse(APIModel):
    """
    工具规划响应。

    `requires_approval=True` 只代表必须进入审批流程，
    并不代表 ApprovalRequest 已写入数据库。
    """

    tool_name: str
    display_name: str
    risk_level: RiskLevel

    arguments: dict[str, Any]
    reason: str

    requires_approval: bool
    verification_hint: str | None = None


class ToolExecutionResponse(APIModel):
    """标准化工具调用结果。"""

    tool_name: str
    success: bool
    verified: bool

    result: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None

    verification: dict[str, Any] | None = None
    retryable: bool = False
    error_code: str | None = None
