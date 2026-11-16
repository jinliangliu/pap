# app/tenancy/rls.py
"""PostgreSQL Row Level Security 的事务级上下文。

应用层（app/tenancy/scope.py）已经把 ORM 查询钉在单个租户上；RLS 是第二道防线：
即使有人写了裸 SQL、或者在 Core 语句里漏了 where，数据库也只返回当前租户的行。

生效条件（缺一不可，部署时必须确认）：

1. 业务库是 PostgreSQL；
2. 迁移 `..._multi_tenant` 已对业务表执行 ENABLE/FORCE ROW LEVEL SECURITY；
3. 应用使用的数据库角色**不是超级用户、也没有 BYPASSRLS**——
   超级用户会直接绕过 RLS，等于这道防线不存在。

事务开始（begin 事件）时按当前租户 `set_config('app.tenant_id', ..., true)`，
`true` 表示事务级：提交/回滚后自动失效，不会污染连接池里的下一条请求。
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tenancy.context import SYSTEM_TENANT, current_tenant

logger = logging.getLogger(__name__)

_SET_TENANT_SQL = "SELECT set_config('app.tenant_id', %s, true)"


def install_tenant_rls_hook(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine.sync_engine, "begin")
    def _set_tenant_context(connection) -> None:  # noqa: ANN001
        tenant = current_tenant()

        # 未进入租户或处于 system 作用域时不设置：
        # RLS 策略里 current_setting 取不到值会得到 NULL，查询结果为空，
        # 是「拒绝」而不是「放行」。维护任务因此必须逐租户处理。
        if tenant is None or tenant == SYSTEM_TENANT:
            return

        connection.exec_driver_sql(_SET_TENANT_SQL, (tenant,))

        # 用户维度也写进会话变量，便于后续给「用户私有」数据加策略。
        # user_id 是 UUID 字符串，这里不做 cast，策略里按需 ::text 比较。
        logger.debug(
            "已设置 RLS 租户上下文。",
            extra={"event": "rls_tenant_context_set", "tenant_id": tenant},
        )
