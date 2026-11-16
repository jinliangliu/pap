# scripts/init_checkpointer.py
"""初始化 LangGraph checkpoint 表结构（由管理账号执行）。

为什么要单独一个脚本：生产环境里应用的数据库角色只有 DML 权限，
**没有 schema 的 CREATE 权限**——这正是 PostgreSQL RLS 作为第二道防线成立的前提。
应用运行时如果能建表，就等于给了它绕开 RLS 的能力。

所以 LangGraph 的 `setup()`（内部是 `CREATE TABLE IF NOT EXISTS`）不能在启动时跑，
改由管理账号离线执行。部署顺序必须是：

    1. alembic upgrade head                    # 业务表
    2. python -m scripts.init_checkpointer     # checkpoint 表
    3. systemctl restart personal-agent-platform

脚本是幂等的：表已存在时只补跑缺失的迁移版本，可以重复执行。

用法：
    python -m scripts.init_checkpointer
    python -m scripts.init_checkpointer --url postgresql://agent:...@127.0.0.1:5432/personal_agent

两者都不给时回落到 settings.resolved_checkpoint_database_url（即 .env 里配置的库）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

_TABLE_QUERY = (
    "SELECT tablename FROM pg_tables "
    "WHERE schemaname = 'public' AND tablename LIKE 'checkpoint%' ORDER BY tablename"
)


def _normalize(url: str) -> str:
    """把 SQLAlchemy 方言前缀换成 psycopg 能直接用的形式。"""
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


async def _run(url: str) -> int:
    pool = AsyncConnectionPool(
        conninfo=_normalize(url),
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=15)

    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()

        async with pool.connection() as conn:
            cursor = await conn.execute(_TABLE_QUERY)
            tables = [row["tablename"] for row in await cursor.fetchall()]

        print(f"checkpoint 表已就绪：{', '.join(tables) or '(未找到)'}")

        expected = {
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        }
        missing = expected - set(tables)

        if missing:
            print(f"缺少表：{', '.join(sorted(missing))}", file=sys.stderr)
            return 1

        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 LangGraph checkpoint 表结构")
    parser.add_argument(
        "--url",
        default="",
        help="管理账号连接串；留空则使用 .env 中的 CHECKPOINT_DATABASE_URL / DATABASE_URL",
    )
    args = parser.parse_args(argv)

    url = (args.url or settings.resolved_checkpoint_database_url).strip()

    # 先归一化再校验：管理账号连接串普遍带方言前缀（/root/.pap-admin-db-url 里是
    # postgresql+psycopg://），只认 postgresql:// 会把这种正当输入当成没给 URL。
    if not _normalize(url).startswith(("postgresql://", "postgres://")):
        print(
            "checkpoint 只能用 PostgreSQL：请提供 --url 或配置 CHECKPOINT_DATABASE_URL。",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_run(url))


if __name__ == "__main__":
    raise SystemExit(main())
