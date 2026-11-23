# app/schemas/connector.py
"""连接器的接口契约。

一条贯穿的规则：**任何响应里都没有凭据明文**，只有 `credential_set: bool`。
这与 `mcp_registered_tools` 的 `token_set` 同一口径，但更严一档：`token_set`
是平台级字段，而这里的凭据按租户存，所以连"有没有配"都要按调用者所在租户算。
"""

from __future__ import annotations

import datetime

from pydantic import Field

from app.models.connector import ConnectorAuthType, ConnectorKind
from app.schemas.common import APIModel


class ConnectorItem(APIModel):
    id: int
    name: str
    display_name: str
    description: str = ""
    kind: ConnectorKind
    base_url: str = ""
    auth_type: ConnectorAuthType = ConnectorAuthType.NONE
    auth_header_name: str = ""
    timeout_seconds: int = 30
    enabled: bool = True

    # 健康状态：unknown 是初始态，不等于健康。没检查过就说正常，
    # 会让"从没配过凭据"和"刚刚测通"在界面上长得一模一样。
    health_status: str = "unknown"
    health_message: str = ""
    health_checked_at: datetime.datetime | None = None

    # 下面两个是**按调用者所在租户**算的，不是全局值。
    credential_set: bool = False
    tool_count: int = 0

    created_by: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


class ConnectorListResponse(APIModel):
    items: list[ConnectorItem] = Field(default_factory=list)


class ConnectorCreateRequest(APIModel):
    name: str
    display_name: str = ""
    description: str = ""
    kind: ConnectorKind = ConnectorKind.MCP
    base_url: str = ""
    auth_type: ConnectorAuthType = ConnectorAuthType.NONE
    auth_header_name: str = ""
    timeout_seconds: int = 30


class ConnectorUpdateRequest(APIModel):
    """全部可选，只处理传进来的字段。

    用 `model_fields_set` 区分「没传」与「显式传 null」：前者是不改，
    后者在 `enabled` 上没有意义，但在 `auth_header_name` 上表示清空。
    """

    display_name: str | None = None
    description: str | None = None
    kind: ConnectorKind | None = None
    base_url: str | None = None
    auth_type: ConnectorAuthType | None = None
    auth_header_name: str | None = None
    timeout_seconds: int | None = None
    enabled: bool | None = None


class ConnectorCredentialRequest(APIModel):
    """凭据写入。

    请求体里带明文是**可以的**：这是服务端到服务端的写入，不经过模型、
    不进对话、不进日志。真正要防的是「让用户在聊天框里把 token 发出来」，
    那条路径在 Studio 助手里被明确拒绝（见 studio_admin_tools 的凭据守卫）。
    """

    secret: str
    username: str | None = None


class ConnectorCredentialResponse(APIModel):
    connector: str
    credential_set: bool
    message: str
    # 保存凭据会顺带复查一次连通性（见 routes/connectors.set_credential）。
    # 把结论单独给出来，前端才能把「存进去了但对端不通」显示成告警，
    # 而不是一句看起来一样的「保存成功」。
    health_status: str = "unknown"


class ConnectorHealthResponse(APIModel):
    connector: str
    status: str
    message: str
    checked_at: datetime.datetime | None = None


class ConnectorRemoteTool(APIModel):
    remote_name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    # 已经接入的会带上本地名，界面上据此显示「已接入 / 可接入」。
    bound_local_name: str | None = None


class ConnectorDiscoverResponse(APIModel):
    connector: str
    endpoint: str
    tools: list[ConnectorRemoteTool] = Field(default_factory=list)


class ConnectorBindItem(APIModel):
    remote_name: str
    local_name: str
    display_name: str = ""
    description: str = ""
    risk_level: str = "read"


class ConnectorBindRequest(APIModel):
    items: list[ConnectorBindItem] = Field(min_length=1)


class ConnectorBindResponse(APIModel):
    connector: str
    bound: list[str] = Field(default_factory=list)
    message: str = ""
