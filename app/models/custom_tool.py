# app/models/custom_tool.py
"""管理员在控制台创建的自定义 HTTP Tool（平台级）。

为什么不放租户表：工具注册表本身是全局的（ToolRegistry 只有一份），
模型可见性由每租户策略再过滤一层。把定义挂在租户下会出现
「同一工具在不同租户里 URL 不同」的歧义，而这层能力本来就不该跨租户分叉。

表里只存「怎么调」的描述，不存任何密钥。需要鉴权的接口一律走
租户工具策略里的 credential_ref（环境变量名），密钥不落这张表。
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.approval import RiskLevel

# 参数位置：决定这个字段最终进 URL 路径、查询串、请求体还是请求头。
PARAM_LOCATIONS = ("path", "query", "body", "header")

# 只支持这几种方法。HEAD/OPTIONS 对工具没有意义，CONNECT 更不能放开。
SUPPORTED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class CustomTool(Base):
    __tablename__ = "custom_tools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 工具名会进模型目录、审批记录与审计，必须是稳定的标识符。
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    method: Mapped[str] = mapped_column(String(8), default="GET", nullable=False)
    # URL 可能含 {placeholder}；占位符必须与 path 参数同名。
    url: Mapped[str] = mapped_column(String(1024), nullable=False)

    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel),
        default=RiskLevel.READ,
        nullable=False,
    )

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    # [{"name": "station_id", "location": "path", "type": "string",
    #   "required": true, "description": "...", "enum": [...]}]
    params: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # 静态请求头。只允许非敏感头；密钥请走租户凭据。
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 所属连接器。为空 = 独立工具（v1 行为：URL 自带、凭据走 credential_ref）。
    #
    # 有连接器之后，`url` 可以写成相对路径（`/v1/stations`），由连接器的
    # `base_url` 补全；凭据也从连接器取，不再依赖环境变量。
    # 不改存量的 `url` 写法：已经能用的配置不该因为加了一层而失效。
    connector_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
