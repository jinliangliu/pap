# app/models/mcp_registered.py
"""管理员在控制台「接入」的远端 MCP Tool（平台级）。

为什么要有这张表：MCP 的远端清单是外部事实，无法预期；而代码里的
`MCP_TOOL_SPECS` 是安全闸门——远端有什么不等于模型能看到什么。
每次新增一个远端工具都改代码发版，等于把「要不要暴露、以什么风险等级暴露」
这个判断挤到发版流程里，管理员只能等。

这张表把闸门从「代码白名单」扩成「代码白名单 + 管理员登记」：
- 代码里登记的工具照旧，且优先级更高（同名时以代码为准，不允许被配置覆盖）；
- 管理员登记的工具必须显式填写本地名、中文说明、风险等级，默认进入
  `tenant_tool_policies` 的默认策略（启用），随时可按租户停用；
- 风险等级为写及以上时，审批由 ToolGateway 单点推导，表里不存
  `requires_approval`——两处都存，早晚会不一致。

每条接入项自带端点（`server_url`）。v1 假设「整个平台只有一条 MCP 通道」，
地址与令牌都取 .env；接入第二个服务时这个假设就塌了：发现是照着 A 地址做的，
接入却拿 .env 的 B 地址去校验，得到的错误是「远端没有这个工具」——
明明存在，却说是下线或改名。所以端点跟着接入项走，运行时也照着它调用。
`server_url` 为空表示沿用 .env 的 MCP_SERVER_URL（v1 的行为）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.approval import RiskLevel


class McpRegisteredTool(Base):
    __tablename__ = "mcp_registered_tools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 本地工具名：进模型目录、审批记录与审计，必须是稳定标识符。
    local_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # 远端工具名：MCP Server 上真实的名字，调度时原样使用。
    remote_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # 该工具所属的 MCP 端点。为空表示沿用 .env 的 MCP_SERVER_URL。
    server_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 该端点的 Bearer Token。空值的含义由 server_url 决定：
    #   与默认端点相同 → 运行时不看这里，仍按租户取 .env 里的令牌；
    #   其它端点       → 该端点不需要鉴权，不发送 Authorization 头。
    # 明文存储的理由与 model_providers.api_key 一致：库本身受运行角色约束，
    # 接口只回 token_set，任何地方都不回显明文。
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel),
        default=RiskLevel.READ,
        nullable=False,
    )

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 所属连接器。为空 = 沿用本行的 `server_url` / `token`（v1 行为）。
    #
    # 有连接器时，端点取 `connectors.base_url`、令牌取租户自己的
    # `connector_credentials`——本行的 `server_url` / `token` 不再参与。
    # 「连接器赢」而不是「行上优先」：两处都能指定端点时，后改的那处不生效
    # 是最难查的一类错；与其定一个谁优先的规则，不如让连接器完全接管。
    connector_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
