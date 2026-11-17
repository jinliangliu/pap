# app/schemas/admin.py
"""平台管理通道的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.approval import RiskLevel
from app.models.tenant import TenantStatus
from app.schemas.common import APIModel


class TenantCreateRequest(APIModel):
    tenant_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="租户唯一标识，会进入键名与对象路径。",
    )
    name: str = Field(min_length=1, max_length=128)
    daily_chat_requests: int = Field(default=0, ge=0, le=10_000_000)
    tool_calls_per_minute: int = Field(default=0, ge=0, le=1_000_000)


class TenantResponse(APIModel):
    tenant_id: str
    name: str
    status: TenantStatus
    created_at: datetime


class TenantStatusRequest(APIModel):
    status: TenantStatus


class MemberUpsertRequest(APIModel):
    subject: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=64)


class MemberResponse(APIModel):
    subject: str
    display_name: str
    role: str
    enabled: bool


class QuotaRequest(APIModel):
    daily_chat_requests: int = Field(ge=0, le=10_000_000)
    tool_calls_per_minute: int = Field(ge=0, le=1_000_000)
    knowledge_max_documents: int = Field(default=0, ge=0, le=1_000_000)


class QuotaResponse(APIModel):
    tenant_id: str
    daily_chat_requests: int
    tool_calls_per_minute: int
    knowledge_max_documents: int


class ToolPolicyRequest(APIModel):
    """单个租户工具策略。保留 `enabled` 是为了兼容既有调用方（脚本、旧页面）。

    `action` 存在时以它为准：`enabled` 只能表达「开 / 关」，而三态策略里
    「需确认」是第三种结局，压不进布尔。映射关系见 `set_tool_policy` 路由。
    """

    enabled: bool = True
    action: str | None = Field(default=None, description="allow / confirm / deny")
    risk_override: str | None = Field(default=None, description="read / write；只能上调")
    allowed_roles: list[str] = Field(default_factory=list, max_length=16)
    credential_ref: str | None = Field(
        default=None,
        max_length=128,
        description="环境变量名，不是密钥本身。",
    )


class ToolPolicyResponse(APIModel):
    tenant_id: str
    tool_name: str
    enabled: bool
    action: str | None = None
    risk_override: str | None = None
    allowed_roles: list[str]
    credential_ref: str | None = None


class AuditItem(APIModel):
    id: str
    event_type: str
    actor_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    success: bool
    message: str | None = None
    # 事件的结构化细节：拒绝原因、生效上限（`limits`）、转交来源、改前改后。
    #
    # 这一列过去只存在库里：接口不回，于是「为什么这次转交被拒」「当时的上限
    # 是多少」只能 psql 才能回答——而回答这类问题正是审计页存在的理由。
    # 内容按写入时的口径，只放脱敏字段（参数只留键名，见 DbAuditSink）。
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class McpRuntimeResponse(APIModel):
    """MCP 运行时配置（只读）。

    `server_url` 是默认端点，值来自进程环境变量：改它要改 .env 并重启。
    接入项可以自带别的端点（见 /mcp/tools 的 server_url），那部分不需要重启。
    """

    enabled: bool
    transport: str = "streamable-http"
    server_url: str | None = None
    connect_timeout_seconds: float = 15.0
    max_result_chars: int = 12000
    token_configured: bool = False
    token_tenants: list[str] = Field(default_factory=list)
    registered_count: int = 0
    note: str = ""


class McpDiscoverRequest(APIModel):
    """用临时连接参数探测远端工具清单。只读，不落库。"""

    url: str | None = Field(default=None, max_length=512)
    token: str | None = Field(default=None, max_length=512)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)


class McpRemoteTool(APIModel):
    name: str
    description: str = ""
    # 已在本地表里登记时给出对应的本地工具名；未登记则为空，
    # 这类远端工具模型永远看不到。
    local_name: str | None = None
    # 是否由管理员接入（可以「取消接入」）。代码白名单里的工具为 False：
    # 它们要下线只能改代码，不能在控制台里被点掉。
    removable: bool = False


class McpDiscoverResponse(APIModel):
    tools: list[McpRemoteTool] = Field(default_factory=list)
    count: int = 0
    registered_count: int = 0
    message: str = ""
    # 这次发现实际连的端点（表单留空时就是 .env 里的地址）。
    # 前端拿它去接入，才不会出现「照着 A 发现、拿 B 校验」的错位。
    server_url: str | None = None


class McpToolRegisterRequest(APIModel):
    """把一个远端工具接入本地白名单。

    本地名不许与已注册工具重名：注册表只有一份，允许重名等于允许
    配置覆盖代码实现。风险等级由管理员显式选，写及以上的审批
    由 ToolGateway 按等级推导，这里不提供「免审批」开关。

    `server_url` / `token` 就是这个工具将来真正调用的端点与凭据；
    留空分别表示「用 .env 的 MCP_SERVER_URL」和「该端点不鉴权」。
    """

    remote_name: str = Field(min_length=1, max_length=128)
    local_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
        description="模型看到并调用的工具名，稳定标识符。",
    )
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    risk_level: RiskLevel = Field(
        default=RiskLevel.READ,
        description="写及以上会自动走审批，配不了免审批。",
    )
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    server_url: str | None = Field(default=None, max_length=512)
    token: str | None = Field(default=None, max_length=512)


class McpRegisteredToolResponse(APIModel):
    id: int
    local_name: str
    remote_name: str
    display_name: str
    description: str = ""
    risk_level: RiskLevel
    requires_approval: bool = False
    timeout_seconds: int = 60
    enabled: bool = True
    # 是否真的在网关里（停用、远端不可达时会为 False）
    registered: bool = False
    server_url: str | None = None
    # 令牌只回「有没有」，不回明文。
    token_set: bool = False
    created_by: str | None = None
    created_at: datetime


class McpRegisteredToolListResponse(APIModel):
    items: list[McpRegisteredToolResponse] = Field(default_factory=list)


class RuntimeSettingsResponse(APIModel):
    """运行配置的当前状态。

    `source` 的存在是为了回答「我看到的是管理员设的，还是 .env 的默认值」。
    只有这两个来源，没有第三种：库里有记录就是 runtime，没有就回落 env。
    """

    observability_enabled: bool
    observability_source: str = Field(
        description="runtime（管理员设过）| env（.env 默认值）",
    )

    # 下面四项属于权限页，跟着同一个快照一起返回：它们都是平台级、
    # 都住 platform_settings，分成两个接口只会让页面多跑一次往返。
    default_tool_action: str = "allow"
    default_tool_action_source: str = "env"
    mcp_inbound_enabled: bool = True
    mcp_inbound_source: str = "env"

    # 入库容量。与上面几项同一份快照：它也是平台级、也住 platform_settings，
    # 再单开一个接口只会让页面多跑一次往返。
    knowledge_max_chunks: int = 2000
    knowledge_max_chunks_source: str = "env"


class RuntimeSettingsUpdate(APIModel):
    # 两项都是「不传 = 不动」。原先是必填，于是只想改容量的一次 PUT 必须
    # 把观测开关的当前值再抄一遍——两个人同时开着这个页面时，后保存的那位
    # 会把别人刚改的观测开关按自己手里的旧值改回去。
    observability_enabled: bool | None = Field(
        default=None,
        description="是否记录 trace / span。关闭后不再新增写入，已有数据保留。",
    )

    knowledge_max_chunks: int | None = Field(
        default=None,
        ge=1,
        le=20000,
        description="单篇文档的切片上限。留空表示不修改。",
    )
