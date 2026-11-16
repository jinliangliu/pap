# app/models/platform_setting.py
"""平台级运行配置（key → JSON 值）。

为何单独一张表、且不挂 RLS：这些是**进程行为**的开关（是否写观测、是否
启用某项增强），不是某个租户的业务数据。放租户表会让「A 租户开了观测、
B 租户没开」变成一种可能，而同一个进程只能有一套行为。

为什么不用 `.env`：`.env` 改了必须重启，且只有能登服务器的人能改。
凡是要「管理员在控制台决定、且改完立刻生效」的项，都走这里。
`.env` 退化成**首次默认值**——库里没有记录时才读它。

现有的租户级运行参数（`tenant_memory_settings`、`tenant_quotas`、
`tenant_tool_policies`）不要往这张表里搬：那些按租户分叉是刻意的。
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    # 用点分段，与接口字段一一对应：observability.enabled
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 存 JSON 而不是 String：布尔、整数、字符串都能放，不必为每个开关加一列。
    value: Mapped[object] = mapped_column(JSON, nullable=False)

    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 记是谁改的。审计表里也有一条，这里留最近一次，页面直接显示。
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
