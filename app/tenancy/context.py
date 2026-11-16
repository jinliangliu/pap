# app/tenancy/context.py
"""当前请求/任务的租户上下文。

两条硬规则：

1. 上下文只能由认证层或显式的服务调用设置，不能来自请求体、模型输出或工具参数；
2. 进入某个租户上下文后，不能再切换到另一个租户——嵌套切换一律报错，
   这是防止「在同一请求里先读 A 再写 B」的最后一道应用层闸门。

`SYSTEM_TENANT` 是唯一绕过过滤的哨兵，仅供维护任务与平台管理员通道使用。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

SYSTEM_TENANT = "__system__"

# 租户 ID 会进入 Redis 键、对象存储路径与线程 ID，必须是受限字符集。
_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_current_tenant: ContextVar[str | None] = ContextVar("pap_current_tenant", default=None)


class TenantIsolationError(RuntimeError):
    """检测到跨租户访问或上下文冲突。"""


class TenantContextMissingError(TenantIsolationError):
    """写租户数据时缺少租户上下文。"""


def normalize_tenant_id(value: str | None) -> str:
    """校验并规范化租户 ID。非法值直接拒绝，不做“尽力修正”。"""
    candidate = (value or "").strip()

    if not _TENANT_ID_PATTERN.match(candidate):
        raise TenantIsolationError("租户标识格式无效。")

    return candidate


def current_tenant() -> str | None:
    """返回当前上下文租户；None 表示未进入任何租户。"""
    return _current_tenant.get()


def in_system_scope() -> bool:
    return _current_tenant.get() == SYSTEM_TENANT


def bind_tenant(tenant_id: str) -> Token:
    """把租户绑定到当前执行流，返回可用于恢复的 token。

    给 FastAPI 依赖使用：`yield` 跨越整个请求，无法用 with 管理器包住。
    """
    return _current_tenant.set(normalize_tenant_id(tenant_id))


def unbind_tenant(token: Token) -> None:
    _current_tenant.reset(token)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    """进入某个租户上下文。

    已经是同一租户时可重入；跨租户嵌套直接报错。
    已处于 system 作用域时允许收窄到具体租户（维护任务逐租户处理）。
    """
    target = normalize_tenant_id(tenant_id)
    active = _current_tenant.get()

    if active not in (None, SYSTEM_TENANT, target):
        raise TenantIsolationError(
            f"拒绝在同一执行流内切换租户：{active} -> {target}"
        )

    token = _current_tenant.set(target)

    try:
        yield
    finally:
        _current_tenant.reset(token)


@contextmanager
def system_tenant_scope(*, allow_from_tenant: bool = False) -> Iterator[None]:
    """跨租户作用域，仅供维护任务与平台管理员通道。

    默认拒绝从具体租户上下文进入：管理通道必须显式声明 `allow_from_tenant=True`，
    让「谁在什么时候跨了租户」在代码里可检索、在审计里可追踪。
    """
    active = _current_tenant.get()

    if active not in (None, SYSTEM_TENANT) and not allow_from_tenant:
        raise TenantIsolationError(
            "禁止从租户上下文直接进入跨租户作用域；请走平台管理员通道。"
        )

    token = _current_tenant.set(SYSTEM_TENANT)

    try:
        yield
    finally:
        _current_tenant.reset(token)


@contextmanager
def platform_admin_tenant_scope(tenant_id: str) -> Iterator[None]:
    """平台管理员以某个租户的身份执行操作（受审计）。

    普通请求不能调用：当前上下文必须是平台租户。这样「跨租户」只有一条代码路径，
    且每条路径都在 admin 路由里显式出现、并写 ADMIN_ACTION 审计。
    """
    from app.core.config import settings

    target = normalize_tenant_id(tenant_id)
    active = _current_tenant.get()

    if active not in (SYSTEM_TENANT, settings.platform_tenant_id):
        raise TenantIsolationError(
            "只有平台管理员上下文才能以其他租户身份操作。"
        )

    token = _current_tenant.set(target)

    try:
        yield
    finally:
        _current_tenant.reset(token)

