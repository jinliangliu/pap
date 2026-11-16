# app/models/connector.py
"""外部连接器：把「一个外部系统的接入」变成一等的配置对象。

## 为什么要有这一层

在此之前，接入一个外部系统是「一条工具一条配置」：一个系统的十个接口就是
十条 `custom_tools` 行，端点抄十遍；十个 MCP 工具就是十行
`mcp_registered_tools`，令牌抄十遍。由此产生的三个具体问题：

1. **改地址要改十处，改漏一处就是「部分工具还指旧地址」**，而这种错只有
   等到对应工具被调用时才显形；
2. **凭据无处安放**。`custom_tools` 根本没有凭据字段，只能靠
   `tenant_tool_policies.credential_ref` 指向环境变量——意味着管理员在界面上
   新建一个工具，其实没法给它配凭据，得让运维改 `.env` 再重启；
   `mcp_registered_tools.token` 有字段，但它是**平台级**明文，全租户共用一个
   外部账号，审计上说不清「这条数据是谁的」；
3. **智能体边界只能列工具名**。接一个连接器就多二十个工具名，改一次边界
   `revision` 就 bump 一次，在途审批单的快照跟着作废。

所以这一层做三件事：端点与认证方式收敛到一处、凭据按租户存、边界可以引用
「连接器」而不是逐个工具名。

## 平台级定义 + 租户级凭据，为什么这么切

- `connectors` 是平台级（不继承 `TenantScoped`）：注册表本身只有一份，
  工具名全局唯一，同一工具在不同租户里 URL 不同会直接制造歧义。
  这与 `custom_tools` / `mcp_registered_tools` 的判断一致。
- `connector_credentials` 是租户级：外部账号天然属于某个组织。
  平台级令牌等于「所有人共用一个账号」，出事时无法定位到人。
  这一层同时补上了 `docs/MCP_INTEGRATION.md` §2.1 里明确列为「没有做」的
  「按租户区分外部端点凭据」。

## 凭据存明文吗

存明文，与 `model_providers.api_key` 同一口径：库本身受运行角色约束，
接口只回 `secret_set`，任何地方都不回显、不写日志、不进审计、不进模型上下文。
不发明一套自制的加密：没有 KMS 的「加密」只是把明文换了位置，还会让人误以为
它有额外保护。

## `health_status` 为什么是 String 不是 Enum

健康状态是运维派生量，不是业务状态机，而且以后大概率还会加值（比如
`degraded`）。PG 上给原生枚举加值要走 `ALTER TYPE ... ADD VALUE`，
不能在事务块里回滚，迁移和回滚的写法都不好看。用 String + 常量校验，
代价是库里可能存进意料外的值，收益是加状态不用改类型。
"""

from __future__ import annotations

import datetime
import enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class ConnectorKind(str, enum.Enum):
    """连接器的协议族。决定「怎么发现能力」和「怎么调」。"""

    HTTP = "http"
    MCP = "mcp"


class ConnectorAuthType(str, enum.Enum):
    """凭据怎么放进请求。

    刻意不提供 `oauth2`：授权码流程要浏览器回调、令牌刷新、按用户绑定，
    与「一条服务端配置」不是同一类东西。硬塞进来会得到一个「看起来支持
    OAuth、实际要人工每月换一次 token」的假能力。需要 OAuth 的外部系统，
    先用它的长期令牌接进来，等真有人用再单独做。
    """

    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    BASIC = "basic"


# 健康状态取值。`unknown` 是初始态，不是「健康」——没检查过就说正常，
# 会让「从没配过凭据」和「刚刚测通」在界面上长得一模一样。
HEALTH_UNKNOWN = "unknown"
HEALTH_OK = "ok"
HEALTH_ERROR = "error"
HEALTH_STATUSES = (HEALTH_UNKNOWN, HEALTH_OK, HEALTH_ERROR)


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 进审计、进智能体边界（`allowed_connectors`），必须是稳定标识符。
    # 改名会让已授权它的智能体静默失去能力，所以与 Agent.name 同一条规矩：
    # 名字是标识，显示名才是给人看的那个。
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    kind: Mapped[ConnectorKind] = mapped_column(
        Enum(
            ConnectorKind,
            name="connector_kind",
            # 一律按 value 落库。PG 原生枚举的标签是小写，不指定会写进成员名
            # （HTTP / MCP），查询与运维手写 SQL 全部对不上——`agent_status`
            # 踩过同一个坑。
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )

    # mcp：完整端点（Streamable HTTP，通常以 /mcp 结尾）。
    # http：工具 URL 的前缀，可空；空表示各工具继续写完整 URL（v1 行为）。
    base_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    auth_type: Mapped[ConnectorAuthType] = mapped_column(
        Enum(
            ConnectorAuthType,
            name="connector_auth_type",
            values_callable=lambda cls: [m.value for m in cls],
        ),
        default=ConnectorAuthType.NONE,
        nullable=False,
    )

    # auth_type=header 时的头名（如 X-Api-Key）。其它类型忽略。
    auth_header_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 停用连接器必须让它的工具一起从目录消失，否则模型会挑一个「已下线」的
    # 工具然后拿到一串连接错误。这条判定在 Gateway 里做（见 types.ToolDefinition）。

    health_status: Mapped[str] = mapped_column(
        String(16), default=HEALTH_UNKNOWN, nullable=False
    )
    # 给人看的结论，含可行动信息（「凭据被拒绝」而不是「检测失败」）。
    health_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    health_checked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConnectorCredential(TenantScoped, Base):
    """某租户在某个连接器上的凭据。

    一个租户对一个连接器最多一条：支持「多个凭据」就要有「这次调用用哪条」的
    选择逻辑，而那个选择最终只能由人来做。真需要多账号时，正确做法是建两个
    连接器（`feishu-prod` / `feishu-test`），而不是在一张表里加个 `label`。

    没有「平台级凭据」这一档：那等于回到全租户共用 token 的老问题。
    """

    __tablename__ = "connector_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "connector_id", name="uq_connector_cred_tenant"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    connector_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # basic 认证的用户名。其它类型为空。
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 明文，理由见模块 docstring。接口只回 `secret_set: true`。
    secret: Mapped[str] = mapped_column(String(2048), nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
