# app/models/mixins.py
"""模型级多租户标记。

继承 `TenantScoped` 即可获得三件事：

1. `tenant_id` 列（唯一声明处，避免各表写法不一致）；
2. ORM 查询自动追加 `tenant_id = 当前租户`（见 app/tenancy/scope.py）；
3. 写入时自动填充并经校验，缺上下文直接失败。

不继承它的只有身份平面：users / roles / role_permissions / tenants。
这些表是「谁属于哪个租户」的授权依据，必须能被认证层跨租户读取，
因此不参与业务数据的自动过滤（详见 docs/MULTI_TENANCY.md）。
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class TenantScoped:
    __tenant_scoped__ = True

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
