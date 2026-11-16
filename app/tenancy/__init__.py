# app/tenancy
"""多租户基础设施。

设计取舍（与 docs/MULTI_TENANCY.md 一致）：

- 隔离粒度：共享库 + 共享表 + `tenant_id` 列，默认方案；
- 租户归属只能来自可信身份（JWT 声明或身份网关），不接受请求体或模型输出；
- 应用层用 ORM 事件强制过滤与写入校验，数据库层用 PostgreSQL RLS 兜底；
- 跨租户访问只有一条显式、可审计的管理通道。
"""

from app.tenancy.context import (
    SYSTEM_TENANT,
    TenantContextMissingError,
    TenantIsolationError,
    bind_tenant,
    current_tenant,
    in_system_scope,
    normalize_tenant_id,
    platform_admin_tenant_scope,
    system_tenant_scope,
    tenant_scope,
    unbind_tenant,
)
from app.tenancy.threads import (
    client_thread_id,
    internal_thread_id,
    validate_client_thread_id,
)

__all__ = [
    "SYSTEM_TENANT",
    "TenantContextMissingError",
    "TenantIsolationError",
    "bind_tenant",
    "client_thread_id",
    "current_tenant",
    "in_system_scope",
    "internal_thread_id",
    "normalize_tenant_id",
    "platform_admin_tenant_scope",
    "system_tenant_scope",
    "tenant_scope",
    "unbind_tenant",
    "validate_client_thread_id",
]
