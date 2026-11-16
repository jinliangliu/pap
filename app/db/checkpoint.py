# app/db/checkpoint.py
"""LangGraph Checkpointer（PostgreSQL）。

唯一后端：`AsyncPostgresSaver` + psycopg 连接池。

为什么不用 SQLite：checkpoint 保存的是 interrupt 现场。写操作审批在
`create_and_wait_approval` 中断，确认请求可能落到另一个进程（重启、多副本、
滚动升级）上，进程本地的 SQLite 文件读不到别人的现场，恢复就会失败。
PostgreSQL 让所有实例看到同一份会话状态。

表结构由 `setup()` 幂等创建（checkpoints / checkpoint_blobs /
checkpoint_writes / checkpoint_migrations），与业务表同库不同表，
不参与 Alembic 迁移链。

唯一例外是测试：APP_ENV=test 且没有 PG 时退化为进程内 `InMemorySaver`，
避免单元测试依赖外部数据库。其他环境下连接串不是 PostgreSQL 会直接报错，
不会静默降级。
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

_saver: BaseCheckpointSaver | None = None
_pool: AsyncConnectionPool | None = None


async def init_checkpointer() -> BaseCheckpointSaver:
    global _saver, _pool

    if _saver is not None:
        return _saver

    if settings.checkpoint_uses_postgres:
        url = settings.resolved_checkpoint_database_url

        _pool = AsyncConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=settings.checkpoint_pool_size,
            open=False,
            # autocommit：setup() 的迁移语句不能被外层事务包住；
            # prepare_threshold=0：连接复用时不保留预处理语句，避免经 PgBouncer
            # 之类的连接池时出现 "prepared statement already exists"。
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await _pool.open(wait=True, timeout=15)

        _saver = AsyncPostgresSaver(_pool)

        if settings.checkpoint_auto_setup:
            # 幂等：库已存在时只补跑缺失的迁移版本。
            await _saver.setup()
        else:
            # 生产：应用账号没有 schema CREATE 权限（RLS 的前提），
            # 表结构由管理账号离线执行 `python -m scripts.init_checkpointer`。
            # 这里只做一次存在性探测，避免「表还没建」拖到第一次会话请求才暴露。
            await _verify_checkpoint_schema(_pool)

        logger.info(
            "Checkpointer 已就绪。",
            extra={"event": "checkpointer_ready", "backend": "postgres"},
        )
        return _saver

    if settings.app_env == "test":
        # 仅测试环境：进程内保存，不落盘。
        _saver = InMemorySaver()
        logger.info(
            "Checkpointer 已就绪。",
            extra={"event": "checkpointer_ready", "backend": "memory"},
        )
        return _saver

    raise RuntimeError(
        "检查点必须使用 PostgreSQL：请把 CHECKPOINT_DATABASE_URL（或 DATABASE_URL）"
        "配置为 postgresql:// 连接串，当前解析结果为 "
        f"{settings.resolved_checkpoint_database_url!r}。"
    )


async def _verify_checkpoint_schema(pool: AsyncConnectionPool) -> None:
    """`CHECKPOINT_AUTO_SETUP=false` 时确认表结构已经就位。

    应用运行时不做 DDL，所以「表没建」必须在启动阶段就报出来，
    而不是等第一个会话请求进来才炸。
    """
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1 FROM checkpoint_migrations LIMIT 1")
    except Exception as exc:  # noqa: BLE001  统一包装成启动期配置错误
        raise RuntimeError(
            "checkpoint 表尚未初始化，而 CHECKPOINT_AUTO_SETUP=false。"
            "请用管理账号执行 `python -m scripts.init_checkpointer` 后重启。"
        ) from exc


def get_checkpointer() -> BaseCheckpointSaver:
    if _saver is None:
        raise RuntimeError(
            "Checkpointer 尚未初始化，请在应用 lifespan 中调用 init_checkpointer()。"
        )

    return _saver


def get_checkpoint_pool() -> AsyncConnectionPool | None:
    """供就绪探针做真实连通性检查；内存后端返回 None。"""
    return _pool


async def discard_thread(thread_id: str) -> None:
    """丢掉一条线程的检查点。

    只用于**结果已经同步返回**的一次性线程（委派的子调用）：它的结论已经
    回到发起方，没有恢复价值。留着它，checkpoint 表里就会攒下无数条不对应
    任何会话的孤立线程，而会话清理扫不到它们。

    失败只记日志：这属于卫生问题，不该让一次成功的业务动作变成失败。
    """
    try:
        deleter = getattr(get_checkpointer(), "adelete_thread", None)
        if deleter is not None:
            await deleter(thread_id)
    except Exception:
        logger.warning(
            "检查点清理失败。",
            extra={"event": "checkpoint_discard_failed", "thread_id": thread_id},
        )


async def close_checkpointer() -> None:
    global _saver, _pool

    if _pool is not None:
        await _pool.close()

    _saver = None
    _pool = None
