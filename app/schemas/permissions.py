# app/schemas/permissions.py
"""权限页的请求与响应模型。

一个工具在页面上要同时说清三件事，缺一件人就得去猜：

1. **它本来是什么**（`base_risk` / `method` / `source`）——注册时的事实；
2. **本租户把它改成了什么**（`action` / `risk_override`，未配置则为 None）；
3. **最终生效是什么**（`effective_action` / `requires_approval`）。

只回 1 和 2 的话，人得自己在脑子里过一遍「未配置 + 平台默认 = 什么」，
而这个推导一旦和 Gateway 里的实现有一处不同，页面就成了错误信息源。
所以第 3 组由服务端算好直接给出。
"""

from __future__ import annotations

from pydantic import Field

from app.models.approval import RiskLevel
from app.schemas.common import APIModel


class ToolPermissionItem(APIModel):
    name: str
    display_name: str
    description: str = ""

    # builtin / mcp / custom
    source: str = "builtin"
    # python / http / mcp，用来在列表上打「HTTP」这类标签
    kind: str = "python"
    method: str | None = None
    connector_name: str = ""

    base_risk: RiskLevel
    # 叠加租户风险覆盖之后的风险。只会上调，所以它 >= base_risk。
    effective_risk: RiskLevel

    # 库里配的动作；None = 未配置（跟随平台默认动作）。
    action: str | None = None
    # 最终生效的动作：库里有就用库里的，没有就是平台默认。
    effective_action: str
    requires_approval: bool

    allowed_roles: list[str] = Field(default_factory=list)
    credential_ref: str | None = None
    risk_override: str | None = None

    # 工具自身是否启用（代码里的 enabled）。与租户策略是两层，不能合并。
    enabled: bool = True


class PermissionsSummary(APIModel):
    total: int = 0
    allow: int = 0
    confirm: int = 0
    deny: int = 0
    unconfigured: int = 0


class PermissionsResponse(APIModel):
    tenant_id: str
    default_action: str
    default_action_source: str = "env"
    mcp_inbound_enabled: bool
    mcp_inbound_source: str = "env"
    tools: list[ToolPermissionItem] = Field(default_factory=list)
    summary: PermissionsSummary = Field(default_factory=PermissionsSummary)

    # 允许配置的来源。前端据此决定「全部来源」筛选要不要出现。
    sources: list[str] = Field(default_factory=list)


class ToolPermissionUpdate(APIModel):
    """保存一个工具的策略。

    `action=None` 表示恢复未配置——不是「改成允许」。这两个在平台默认动作
    被切成拒绝时行为完全不同，所以必须可区分。
    """

    action: str | None = Field(default=None, description="allow / confirm / deny；null = 未配置")
    risk_override: str | None = Field(default=None, description="read / write；只能上调")
    allowed_roles: list[str] = Field(default_factory=list, max_length=16)
    credential_ref: str | None = Field(default=None, max_length=128)


class PlatformPermissionsUpdate(APIModel):
    default_action: str = Field(description="allow / deny")
    mcp_inbound_enabled: bool
