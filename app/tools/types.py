# app/tools/types.py
from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.approval import RiskLevel
from app.services.delegation_limits import DelegationLimits

ToolHandler = Callable[
    ["ToolExecutionContext", dict[str, Any]],
    Awaitable["ToolExecutionResult"],
]

ToolVerifier = Callable[
    ["ToolExecutionContext", dict[str, Any]],
    Awaitable["ToolExecutionResult"],
]

# 审批前预检：返回拒绝理由，None 表示放行。
ToolPreflight = Callable[["ToolExecutionContext", dict[str, Any]], Awaitable[str | None]]

ToolKind = Literal[
    "python",
    "http",
    "mcp",
]

ToolSource = Literal["builtin", "mcp", "custom"]


@dataclass(frozen=True, slots=True)
class ConnectorRuntime:
    """本次调用可用的连接器快照，含**该租户自己的**凭据。

    为什么凭据挂在这里而不是前进 handler 闭包：handler 是进程级、跨租户的，
    闭包里塞第一个租户的令牌，第二个租户就会拿着别人的身份去请求外部系统——
    而且从返回值上看不出来，只会发现「数据不对」。

    `secret` 用 `repr=False`：本对象会经 `ToolExecutionContext` 进入异常
    traceback、调试日志与 span 属性。frozen dataclass 的默认 repr 会把字段
    全打出来，一行日志就够把令牌写进长期存储。
    """

    id: int
    name: str
    kind: str
    base_url: str = ""
    auth_type: str = "none"
    auth_header_name: str = ""
    username: str | None = None
    secret: str | None = field(default=None, repr=False)

    def auth_headers(self) -> dict[str, str]:
        """把凭据转成请求头。没有凭据就返回空——**不做**任何回退到环境变量的猜测。"""
        if not self.secret or self.auth_type == "none":
            return {}

        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {self.secret}"}

        if self.auth_type == "header":
            name = (self.auth_header_name or "").strip()
            return {name: self.secret} if name else {}

        if self.auth_type == "basic":
            raw = f"{self.username or ''}:{self.secret}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

        return {}

    def requires_secret(self) -> bool:
        return self.auth_type != "none"



@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """
    由 Gateway 注入工具处理函数的可信执行上下文。

    不应由模型或普通 API 请求自由构造。
    `credential` 是按租户解析出的外部系统凭据，只在本次调用内有效，
    禁止写入日志、审计或模型上下文。
    """

    tenant_id: str
    user_id: str
    thread_id: str
    run_id: str
    request_id: str

    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    # 平台管理员：与路由层的 `_can_manage` 同一语义（`is_platform_admin or scope`）。
    #
    # 不加这一位会出现一道很难查的缝：管理接口按「是不是平台管理员」放行，
    # 工具目录却只认 scopes。结果是同一个人能点按钮改配置，却让助手在对话里
    # 说「我没有权限」——而且模型只会换个说法，不会报错。
    # 只豁免 scope 检查；角色、租户策略、站点授权、智能体边界照旧生效。
    is_platform_admin: bool = False

    # 归属会话：沙箱与产物按租户 + 用户 + 会话绑定。
    conversation_id: str = ""

    # 本条消息上传的附件句柄。
    # 句柄的可解析范围是整个会话（图片和 PDF 落在同一个会话目录），
    # 所以「能读到」不代表「是本轮该读的那个」：工具用它判断模型是否
    # 拿了更早消息里的句柄，而不是静默识别出另一份材料。
    attachment_ids: tuple[str, ...] = ()

    # 审批通过后由审批服务填入，供外部写接口透传。
    approval_id: str | None = None
    idempotency_key: str | None = None
    external_operation_id: str | None = None

    # 租户级凭据（由租户工具策略的 credential_ref 解析）。
    credential: str | None = None

    # 智能体边界。空值 = 平台基线（不额外收窄），与没有这套机制时完全等价。
    #
    # 治理红线在这里落地：这三个字段只会让可用工具**变少**，
    # 永远不会让任何人拿到原本没有的权限。判断顺序是
    # 「人的 scope / 角色 → 智能体边界 → 租户策略」，前两层任一不通过即不可见。
    agent_id: str | None = None
    agent_name: str = ""
    agent_revision: int = 0
    agent_allowed_tools: tuple[str, ...] | None = None
    agent_allowed_skills: tuple[str, ...] | None = None
    # 允许使用的连接器名。与 agent_allowed_tools 是并集：命中任一即可见。
    # 两者都为 None 才表示「不额外收窄」。
    agent_allowed_connectors: tuple[str, ...] | None = None
    agent_can_write: bool = True

    # 委派预算：本 Agent 自己的配置（None = 走平台默认）与**已经生效**的上限。
    #
    # 两者都要有。只看前者的话，A(2 跳) → B(未配) 这条链在 B 那一跳会退回默认 2，
    # 而 A 收窄到 1 这件事就丢了；只看后者则 B 自己的配置永远进不来。
    # 合并规则在 `DelegationLimits.narrowed_by()` 里，取 min。
    agent_max_delegation_depth: int | None = None
    agent_delegation_budget: int | None = None
    delegation_limits: DelegationLimits = DelegationLimits()

    # 本次调用解析出的连接器（含租户凭据），由 Gateway 注入。
    # 为空表示这个工具不经过连接器（内置工具、独立自定义工具）。
    connector: ConnectorRuntime | None = None

    # 委派链：已经进入过的智能体名字，按跳序。空元组 = 本轮是发起方。
    #
    # 三个计数（路径 / 深度 / 本轮次数）都挂在这里而不是放提示词，
    # 因为它们是**执行纪律**，与「模型看到了什么」无关；
    # 混进 state 的可见部分会让模型多出一堆看不懂的字段。
    # 但子调用是一个新的执行图，跨图只能走 state——所以 state 里也存一份，
    # 由 `_context` 还原到这里（见 `docs/AGENT_CONNECT.md` §5）。
    delegation_path: tuple[str, ...] = ()
    delegation_count: int = 0

    # 本 Agent 的上游：从根到直接上级，每项 `(agent_id, name)`。
    #
    # 与 `delegation_path` 不是一回事，别合并：
    # - `delegation_path` 是**已经进过谁**，用于环检测，会在委派成功后被追加；
    # - `delegation_ancestors` 是**谁把这件事交下来的**，只描述上游，
    #   委派出去时不会变。审批单要冻结的是后者——执行期复核问的是
    #   「当初那一跳现在还应不应该发生」，而不是「这个 Agent 走过哪些地方」。
    #
    # 用 `(id, name)` 而不是只留名字：名字在 Agent 删除后可被新 Agent 占用。
    delegation_ancestors: tuple[tuple[str, str], ...] = ()

    # 发起方所在的会话线程。子调用跑在自己的线程上，而写操作的结论要回到
    # 用户看得见的那个会话——两者都要留，且要沿着委派链一路传下去。
    delegation_origin_thread: str = ""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """
    本地受控工具定义。

    工具必须显式注册，模型不能因为知道某个 URL、MCP Server
    或 Python 函数名就自行调用它。
    """

    name: str
    display_name: str
    description: str

    kind: ToolKind
    risk_level: RiskLevel

    input_schema: dict[str, Any]
    handler: ToolHandler

    # 工具来源：builtin 代码注册、mcp 远端白名单、custom 控制台新建。
    # 详情页与「同名冲突」判断都依赖它，不能靠 kind 猜。
    source: ToolSource = "builtin"

    enabled: bool = True

    # 来源连接器（为空 = 独立工具）。
    #
    # `connector_name` 是给智能体边界用的：`allowed_connectors` 里写的是名字，
    # 而判定发生在同步的目录生成路径上，拿不到库。id 用于解析凭据与回写。
    connector_id: int | None = None
    connector_name: str = ""
    # 注册那一刻连接器是否启用。连接器停用后它的工具必须一起消失，
    # 否则模型会挑一个已下线的工具，然后拿到一串连接错误。
    connector_ready: bool = True

    # 允许调用该工具所需的权限；空元组代表不额外要求 Scope。
    required_scopes: tuple[str, ...] = ()

    # 支持按角色限制；空元组代表不额外限制角色。
    allowed_roles: tuple[str, ...] = ()

    # 只读工具可由 Gateway 直接调用；
    # 写操作必须由审批服务在审批后调用 execute_approved()。
    requires_approval: bool = False

    # 工具执行超时（秒），由 Gateway 用 asyncio.wait_for 强制执行。
    timeout_seconds: int = 20

    # 需要站点授权的工具，声明承载 station_id 的参数名。
    # Gateway 会在执行前校验该站点的读/写授权。
    station_id_argument: str | None = None

    # 写操作的回读核验函数；不能回读时不得对外声称已核验。
    verifier: ToolVerifier | None = None

    # 面向用户的核验说明，不包含内部实现细节。
    verification_hint: str | None = None

    # 审批单生成之前的预检：返回拒绝理由，None 表示可以继续。
    #
    # 与 handler 里的校验是两层，不是重复：
    # - 这里决定「要不要让用户看到确认卡」——注定失败的操作不该弹卡片。
    #   让人对着一张一定会失败的卡点确认，训练出的是闭眼确认。
    # - handler 里的校验是权威判定，执行时状态可能已经变了，不能信预检的结论。
    #
    # 只在写路径（`check_resource_access`）调用，报错文案会原样给用户看。
    preflight: ToolPreflight | None = None


@dataclass(frozen=True, slots=True)
class ToolPublicView:
    """暴露给模型的工具视图：不含 handler、URL、鉴权细节。"""

    tool_name: str
    display_name: str
    description: str
    risk_level: RiskLevel
    public_input_schema: dict[str, Any]
    requires_approval: bool

@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """
    工具的标准化业务执行结果。

    重要：
    - success=True：工具侧认为业务调用成功；
    - verified=True：已通过回读或明确证据核验；
    - success=True 且 verified=False：只能说“已提交，尚未核验”；
    - success=False：不能对用户表述为已完成。
    """

    success: bool
    result: dict[str, Any] = field(default_factory=dict)

    message: str | None = None
    verification: dict[str, Any] | None = None
    verified: bool = False

    # 外部系统标识，供恢复时按幂等键回读。
    external_operation_id: str | None = None

    retryable: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallPlan:
    """
    Gateway 对写操作生成的执行计划。

    此对象不表示操作已经执行；它由审批服务转化为 ApprovalRequest，
    并通过 parameter_hash 绑定审批参数。
    """

    tool_name: str
    display_name: str
    risk_level: RiskLevel

    arguments: dict[str, Any]
    reason: str

    requires_approval: bool
    verification_hint: str | None = None
