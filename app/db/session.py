# app/db/session.py
"""唯一的数据库会话入口。

业务库统一使用异步驱动。psycopg3 与 aiosqlite 都支持 SQLAlchemy 异步方言。

这里同时负责两件与租户隔离有关的事：

1. 导入 `app.tenancy.scope`，注册 ORM 的自动过滤/写入校验事件；
2. 给 PostgreSQL 引擎挂上事务级 `app.tenant_id` 设置（RLS 依赖它）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.tenancy.scope  # noqa: F401  导入即注册 ORM 事件，勿删
from app.core.config import settings
from app.tenancy.rls import install_tenant_rls_hook


def _engine_options() -> dict[str, Any]:
    if settings.is_sqlite:
        # SQLite 使用 NullPool，避免跨事件循环复用连接。
        from sqlalchemy.pool import NullPool

        return {"poolclass": NullPool}

    return {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
    }


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    future=True,
    **_engine_options(),
)

install_tenant_rls_hook(engine)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    await engine.dispose()
