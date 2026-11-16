# app/tenancy/scope.py
"""ORM 层强制租户隔离。

三个目标，缺一不可：

- **读**：任何 SELECT 都自动追加 `tenant_id = 当前租户`，不依赖开发者记得写 where；
- **写**：新对象自动填充 tenant_id；显式传入其他租户、或缺少上下文时直接抛错；
- **改**：禁止把已有行的 tenant_id 改到另一个租户（那是最直接的越权手法）。

已知边界（必须清楚，别误以为这里是万能的）：

- Core 语句（`text()` / `update()` / `delete()`）不会自动加条件，
  这类调用必须显式带 tenant_id；数据库层 RLS 是它们的兜底；
- 平台管理员/维护任务通过 `system_tenant_scope()` 显式绕过，且必须留审计。

事件注册在导入本模块时完成；`app/db/session.py` 会确保它被提前导入。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import bindparam, event, inspect
from sqlalchemy.orm import Session, with_loader_criteria

from app.core.config import settings
from app.db.base import Base
from app.tenancy.context import (
    SYSTEM_TENANT,
    TenantContextMissingError,
    TenantIsolationError,
    current_tenant,
)

logger = logging.getLogger(__name__)

# 显式执行选项：允许单条查询跨租户（只给管理/诊断代码用，且必须留审计）。
INCLUDE_ALL_TENANTS = "include_all_tenants"

# 条件里用绑定参数而不是闭包捕获租户：
# SQLAlchemy 会缓存语句结构，闭包值不进缓存键，第一个租户的条件会被后续请求复用。
_TENANT_BIND = "pap_tenant_scope"

_options: list[Any] = []
_options_mapper_count = -1


def _tenant_criteria(cls: Any) -> Any:
    """生成 `tenant_id = :pap_tenant_scope`。

    参数名必须写成字面量，不能引用 `_TENANT_BIND` 常量：
    SQLAlchemy 分析这个可调用对象时，会把它的全局/闭包变量换成 `PyWrapper`
    代理以便追踪引用，`bindparam(_TENANT_BIND)` 拿到的就是 `PyWrapper`
    而不是字符串，编译期会抛 `TypeError: expected string or bytes-like object`。
    """
    return cls.tenant_id == bindparam("pap_tenant_scope")


def _tenant_options() -> list[Any]:
    """覆盖全部租户表的过滤选项。

    选项本身与租户无关（租户值走绑定参数），可以跨语句复用；
    模型数量变化（迟到的模型导入）时重建，避免出现没被过滤的表。
    """
    global _options, _options_mapper_count

    mappers = Base.registry.mappers

    if _options and _options_mapper_count == len(mappers):
        return _options

    _options = [
        with_loader_criteria(mapper.class_, _tenant_criteria, include_aliases=True)
        for mapper in mappers
        if getattr(mapper.class_, "__tenant_scoped__", False)
    ]
    _options_mapper_count = len(mappers)

    return _options


def _is_scoped(obj: Any) -> bool:
    return bool(getattr(type(obj), "__tenant_scoped__", False))


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state: Any) -> None:
    if not state.is_select:
        return

    tenant = current_tenant()

    if tenant is None or tenant == SYSTEM_TENANT:
        return

    if state.execution_options.get(INCLUDE_ALL_TENANTS):
        return

    options = _tenant_options()

    if options:
        state.statement = state.statement.options(*options)
        # 绑定值放在执行参数里，语句结构（含编译缓存）可以安全地跨租户复用。
        state.statement = state.statement.params(**{_TENANT_BIND: tenant})


@event.listens_for(Session, "before_flush")
def _enforce_tenant_on_write(session: Session, flush_context: Any, instances: Any) -> None:
    tenant = current_tenant()
    concrete = tenant is not None and tenant != SYSTEM_TENANT

    for obj in session.new:
        if not _is_scoped(obj):
            continue

        value = obj.tenant_id

        if value is None:
            if concrete:
                obj.tenant_id = tenant
            elif settings.tenant_strict_writes:
                # 既没有上下文、也没有显式租户：这条数据不知道该归谁，必须拒绝。
                # 服务层显式传入 tenant_id 的情况（审批、审计、维护任务）是允许的，
                # 因为调用方已经做过租户校验。
                raise TenantContextMissingError(
                    f"写入 {type(obj).__name__} 时缺少租户上下文，已拒绝。"
                )
        elif concrete and value != tenant:
            raise TenantIsolationError(
                f"拒绝写入其他租户的数据：{type(obj).__name__} "
                f"(tenant_id={value!r}, 当前={tenant!r})"
            )

    for obj in session.dirty:
        if not _is_scoped(obj):
            continue

        state = inspect(obj)

        if state.attrs.tenant_id.history.has_changes():
            raise TenantIsolationError(
                f"禁止修改 {type(obj).__name__} 的 tenant_id。"
            )

        if concrete and obj.tenant_id != tenant:
            raise TenantIsolationError(
                f"拒绝修改其他租户的数据：{type(obj).__name__}"
            )

    for obj in session.deleted:
        if not _is_scoped(obj):
            continue

        if concrete and obj.tenant_id != tenant:
            raise TenantIsolationError(
                f"拒绝删除其他租户的数据：{type(obj).__name__}"
            )
