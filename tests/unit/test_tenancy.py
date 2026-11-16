# tests/unit/test_tenancy.py
"""租户上下文与 ORM 强制隔离。

这些用例直接打在三层防线的前两层上（上下文 + ORM 事件），
不经过 HTTP，因此能覆盖「开发者忘记写 where」这类真实失误。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.tenancy import (
    SYSTEM_TENANT,
    TenantContextMissingError,
    TenantIsolationError,
    bind_tenant,
    client_thread_id,
    current_tenant,
    in_system_scope,
    internal_thread_id,
    normalize_tenant_id,
    platform_admin_tenant_scope,
    system_tenant_scope,
    tenant_scope,
    unbind_tenant,
    validate_client_thread_id,
)
from app.tenancy.scope import INCLUDE_ALL_TENANTS


def _conversation(*, tenant_id: str, thread: str, title: str) -> Conversation:
    return Conversation(
        tenant_id=tenant_id,
        user_id="u1",
        thread_id=thread,
        title=title,
    )


async def _seed() -> None:
    """幂等建两条分属不同租户的会话（thread_id 全局唯一，不能重复插入）。"""
    async with async_session_factory() as session:
        existing = set(
            (
                await session.scalars(
                    select(Conversation.thread_id).where(
                        Conversation.thread_id.in_(["ta:u1:t", "tb:u1:t"])
                    )
                )
            ).all()
        )

        for tenant_id, thread, title in (
            ("ta", "ta:u1:t", "A"),
            ("tb", "tb:u1:t", "B"),
        ):
            if thread in existing:
                continue

            with tenant_scope(tenant_id):
                session.add(_conversation(tenant_id=tenant_id, thread=thread, title=title))
                await session.commit()


async def _titles(tenant_id: str | None, **execution_options) -> set[str]:  # noqa: ANN003
    async with async_session_factory() as session:
        statement = select(Conversation).execution_options(**execution_options)

        if tenant_id is None:
            rows = (await session.scalars(statement)).all()
        else:
            with tenant_scope(tenant_id):
                rows = (await session.scalars(statement)).all()

        return {row.title for row in rows if row.title in {"A", "B"}}


# ---------------- 读 ----------------


async def test_select_is_filtered_by_tenant():
    await _seed()

    assert await _titles("ta") == {"A"}
    assert await _titles("tb") == {"B"}


async def test_filter_survives_statement_cache_reuse():
    """同一语句结构在不同租户间复用，参数不能串。

    用绑定参数而不是闭包捕获租户，正是为了这一点：
    闭包值不进语句缓存键，第二个租户会拿到第一个租户的条件。
    """
    await _seed()

    for _ in range(3):
        assert await _titles("ta") == {"A"}
        assert await _titles("tb") == {"B"}


async def test_no_context_means_no_filter():
    """没有租户上下文时不加条件——这类调用只能来自系统级代码。"""
    await _seed()

    assert await _titles(None) == {"A", "B"}


async def test_system_scope_sees_every_tenant():
    await _seed()

    with system_tenant_scope():
        assert in_system_scope() is True
        assert await _titles(None) == {"A", "B"}


async def test_explicit_opt_in_is_required_for_cross_tenant_query():
    await _seed()

    assert await _titles("ta") == {"A"}
    assert await _titles("ta", **{INCLUDE_ALL_TENANTS: True}) == {"A", "B"}


# ---------------- 写 ----------------


async def test_insert_fills_tenant_id_from_context():
    async with async_session_factory() as session:
        with tenant_scope("tc"):
            row = Conversation(user_id="u1", thread_id="tc:u1:t", title="C")
            session.add(row)
            await session.commit()

    assert row.tenant_id == "tc"


async def test_insert_for_other_tenant_is_refused():
    async with async_session_factory() as session:
        with tenant_scope("ta"):
            session.add(_conversation(tenant_id="tb", thread="x:1", title="X"))

            with pytest.raises(TenantIsolationError):
                await session.commit()


async def test_insert_without_context_is_refused():
    async with async_session_factory() as session:
        session.add(Conversation(user_id="u1", thread_id="no-ctx:1", title="无租户"))

        with pytest.raises(TenantContextMissingError):
            await session.commit()


async def test_tenant_id_cannot_be_rewritten():
    """把已有行改到另一个租户，是最直接的越权手法。"""
    await _seed()

    async with async_session_factory() as session:
        with tenant_scope("ta"):
            row = await session.scalar(
                select(Conversation).where(Conversation.title == "A")
            )
            assert row is not None

            row.tenant_id = "tb"

            with pytest.raises(TenantIsolationError):
                await session.commit()


# ---------------- 上下文 ----------------


async def test_nested_switch_between_tenants_is_refused():
    with tenant_scope("ta"), pytest.raises(TenantIsolationError):
        with tenant_scope("tb"):
            pass  # pragma: no cover - 不应进入


async def test_same_tenant_scope_is_reentrant():
    with tenant_scope("ta"), tenant_scope("ta"):
        assert current_tenant() == "ta"


async def test_system_scope_cannot_be_entered_from_tenant():
    with tenant_scope("ta"), pytest.raises(TenantIsolationError):
        with system_tenant_scope():
            pass  # pragma: no cover - 不应进入


async def test_platform_admin_scope_requires_platform_context():
    with pytest.raises(TenantIsolationError):
        with platform_admin_tenant_scope("ta"):
            pass  # pragma: no cover - 不应进入

    # 平台租户上下文是唯一入口。
    from app.core.config import settings

    with tenant_scope(settings.platform_tenant_id):
        with platform_admin_tenant_scope("ta"):
            assert current_tenant() == "ta"

        assert current_tenant() == settings.platform_tenant_id


async def test_bind_and_unbind_restore_context():
    token = bind_tenant("ta")
    assert current_tenant() == "ta"
    unbind_tenant(token)
    assert current_tenant() is None


def test_tenant_id_must_be_a_safe_token():
    assert normalize_tenant_id("  tenant-a ") == "tenant-a"

    for bad in ("", "tenant/../a", "租户", "a b", "-leading", "x" * 65):
        with pytest.raises(TenantIsolationError):
            normalize_tenant_id(bad)


def test_system_tenant_is_not_a_valid_business_tenant():
    """`__system__` 是内部哨兵，业务代码不能把它当租户 ID 传进来。

    字符集校验（首字符必须是字母或数字）恰好挡住了它：
    否则任何能控制租户头的地方都能申请「系统作用域」，直接绕过全部过滤。
    """
    with pytest.raises(TenantIsolationError):
        normalize_tenant_id(SYSTEM_TENANT)

    with pytest.raises(TenantIsolationError):
        with tenant_scope(SYSTEM_TENANT):
            pass  # pragma: no cover - 不应进入


# ---------------- 线程命名空间 ----------------


def test_thread_id_is_namespaced_by_tenant_and_user():
    a = internal_thread_id(tenant_id="ta", user_id="u1", client_id="c1")
    b = internal_thread_id(tenant_id="tb", user_id="u1", client_id="c1")
    c = internal_thread_id(tenant_id="ta", user_id="u2", client_id="c1")

    assert len({a, b, c}) == 3
    assert client_thread_id(a) == "c1"
    assert client_thread_id(b) == "c1"


def test_client_thread_id_is_validated():
    for bad in ("", "../etc/passwd", "a b", "-x", "x" * 129):
        with pytest.raises(ValueError, match=""):
            validate_client_thread_id(bad)

    assert validate_client_thread_id("thread-1:ok") == "thread-1:ok"


# ---------------- 枚举落库表示 ----------------


async def test_tenant_status_is_stored_and_read_as_enum_value():
    """回归：`tenants.status` 必须按 value（小写）落库。

    线上真实故障：迁移脚本与运维 SQL 写的是 `'active'`，而 SQLAlchemy `Enum`
    默认按成员名（`'ACTIVE'`）读回，于是 `GET /me` 直接 `KeyError: 'active'`。

    本地 `create_all` 建的库两边都由 ORM 生成，表现一致，所以只有跑过 Alembic
    的 PostgreSQL 库才会暴露——这个用例把「按 value 读写」钉死。
    """
    from sqlalchemy import text

    from app.models.tenant import Tenant, TenantStatus

    async with async_session_factory() as session:
        with tenant_scope("enum-probe"):
            # 写入：ORM 生成的值必须是小写
            session.add(Tenant(id="enum-write", name="写入探针", status=TenantStatus.SUSPENDED))
            await session.flush()

            stored = await session.scalar(
                text("select status from tenants where id = 'enum-write'")
            )
            assert stored == "suspended"

            # 读取：迁移/运维脚本写入的小写值必须能反转成枚举
            await session.execute(
                text(
                    "insert into tenants (id, name, status, created_at, updated_at) "
                    "values ('enum-read', '读取探针', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            tenant = await session.get(Tenant, "enum-read")

            assert tenant is not None
            assert tenant.status is TenantStatus.ACTIVE
            assert tenant.is_active()

            await session.rollback()
