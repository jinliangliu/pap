# scripts/purge_orphan_traces.py
"""清理已删会话留下的痕迹。

V1.4.0 之前，删除会话只删 `conversations` 与 `messages`：

- `agent_runs.conversation_id` 是 `ON DELETE SET NULL`，记录留着，原话摘要也留着；
- LangGraph 的 checkpoint 按 thread_id 存，不随会话删除；
- 会话产物（artifacts）同样没有清理。

结果是「已经删掉的会话」仍然能从图状态里读出完整对话与 OCR 正文。
本脚本找出这些孤儿痕迹，默认只报告，`--apply` 才真正删除。

判定很保守：
- 线程必须**在 conversations 表里完全不存在**才进入候选；
- 只处理有 run 记录、且最后活动早于 `--min-age-minutes` 的线程，
  避免把「会话行尚未提交、图已经开始跑」的进行中请求当成孤儿；
- 没有 run 记录的线程只报告，不动手——无法判断它是不是正在用。

用法：
    python -m scripts.purge_orphan_traces                 # 只报告
    python -m scripts.purge_orphan_traces --apply         # 真正删除
    python -m scripts.purge_orphan_traces --min-age-minutes 60
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.storage.object_store import ObjectStoreError, get_object_store
from app.tenancy.context import tenant_scope

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("purge_orphan_traces")

_CHECKPOINT_TABLES = ("checkpoint_blobs", "checkpoint_writes", "checkpoints")


async def _collect(*, min_age_minutes: int, include_unverified: bool = False) -> dict:
    """按租户收集会话与运行记录，再和 checkpoint 线程做差集。

    agent_runs 挂 RLS，没有租户上下文时查询会静默返回空集，
    所以必须逐租户切换上下文，不能一条全局 SQL 查完。
    """
    async with async_session_factory() as session:
        tenant_ids = [str(row) for row in (await session.scalars(select(Tenant.id))).all()]

    live_threads: set[str] = set()
    orphan_runs: dict[str, dict] = {}

    for tenant_id in tenant_ids:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    live_threads |= set(
                        (await session.scalars(text("SELECT thread_id FROM conversations"))).all()
                    )
                    rows = (
                        await session.execute(
                            text(
                                "SELECT thread_id, count(*) AS n, max(started_at) AS last_at "
                                "FROM agent_runs WHERE conversation_id IS NULL "
                                "GROUP BY thread_id"
                            )
                        )
                    ).all()

        for thread_id, count, last_at in rows:
            orphan_runs[thread_id] = {"tenant_id": tenant_id, "runs": count, "last_at": last_at}

    async with async_session_factory() as session:
        checkpoint_rows = (
            await session.execute(
                text(
                    "SELECT thread_id, count(*) AS n FROM checkpoints GROUP BY thread_id"
                )
            )
        ).all()

    checkpoint_counts = {thread_id: count for thread_id, count in checkpoint_rows}
    cutoff = datetime.now(UTC) - timedelta(minutes=min_age_minutes)

    purgeable: list[dict] = []
    manual: list[dict] = []

    for thread_id, checkpoints in checkpoint_counts.items():
        if thread_id in live_threads:
            continue

        run = orphan_runs.get(thread_id)

        if run is None:
            item = {"thread_id": thread_id, "checkpoints": checkpoints}
            if include_unverified:
                purgeable.append({**item, "runs": 0, "last_at": None, "tenant_id": None})
            else:
                manual.append(item)
            continue

        last_at = run["last_at"]
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)

        if last_at is not None and last_at > cutoff:
            manual.append(
                {
                    "thread_id": thread_id,
                    "checkpoints": checkpoints,
                    "reason": "最近仍在活动",
                }
            )
            continue

        purgeable.append(
            {
                "thread_id": thread_id,
                "checkpoints": checkpoints,
                "runs": run["runs"],
                "last_at": last_at,
                "tenant_id": run["tenant_id"],
            }
        )

    # 会话已不存在、但产物记录还指向它的，一并算孤儿
    orphan_artifacts: list[str] = []

    for tenant_id in tenant_ids:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    orphan_artifacts += list(
                        (
                            await session.scalars(
                                text(
                                    "SELECT a.object_key FROM artifacts a "
                                    "WHERE a.conversation_id IS NOT NULL "
                                    "AND NOT EXISTS (SELECT 1 FROM conversations c "
                                    "WHERE CAST(c.id AS text) = a.conversation_id)"
                                )
                            )
                        ).all()
                    )

    return {
        "tenants": tenant_ids,
        "live_threads": live_threads,
        "checkpoint_threads": checkpoint_counts,
        "purgeable": purgeable,
        "manual": manual,
        "orphan_artifacts": orphan_artifacts,
    }


async def _apply(report: dict) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            for table in _CHECKPOINT_TABLES:
                for item in report["purgeable"]:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE thread_id = :tid"),
                        {"tid": item["thread_id"]},
                    )

    for item in report["purgeable"]:
        if item["tenant_id"] is None:
            continue
        with tenant_scope(item["tenant_id"]):
            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "DELETE FROM agent_runs "
                            "WHERE conversation_id IS NULL AND thread_id = :tid"
                        ),
                        {"tid": item["thread_id"]},
                    )

    if report["orphan_artifacts"]:
        for tenant_id in report["tenants"]:
            with tenant_scope(tenant_id):
                async with async_session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "DELETE FROM artifacts a WHERE a.conversation_id IS NOT NULL "
                                "AND NOT EXISTS (SELECT 1 FROM conversations c "
                                "WHERE CAST(c.id AS text) = a.conversation_id)"
                            )
                        )

        # 记录删了，文件还在等于没清干净；删不掉的只记日志，不中断。
        store = get_object_store()
        for key in report["orphan_artifacts"]:
            try:
                await store.delete(key)
            except (ObjectStoreError, OSError):
                logger.warning("删除孤儿产物失败：%s", key)


async def _run(args: argparse.Namespace) -> int:
    report = await _collect(
        min_age_minutes=args.min_age_minutes,
        include_unverified=args.include_unverified,
    )

    logger.info("租户 %d 个 | 会话线程 %d 个", len(report["tenants"]), len(report["live_threads"]))
    logger.info("checkpoint 线程 %d 个", len(report["checkpoint_threads"]))
    logger.info("")

    if report["purgeable"]:
        logger.info("可清理的孤儿线程：%d 个", len(report["purgeable"]))
        for item in report["purgeable"]:
            logger.info(
                "  %-60s checkpoint=%-4d run=%-3d 最后活动=%s",
                item["thread_id"],
                item["checkpoints"],
                item["runs"],
                item["last_at"].isoformat() if item["last_at"] else "未记录",
            )
    else:
        logger.info("没有可清理的孤儿线程。")

    if report["manual"]:
        logger.info("")
        logger.info("需要人工确认（未处理）：%d 个", len(report["manual"]))
        for item in report["manual"]:
            logger.info(
                "  %-60s checkpoint=%-4d %s",
                item["thread_id"],
                item["checkpoints"],
                item.get("reason", "没有 run 记录，无法判断是否在用"),
            )

    if report["orphan_artifacts"]:
        logger.info("")
        logger.info("孤儿产物：%d 个", len(report["orphan_artifacts"]))
        for key in report["orphan_artifacts"]:
            logger.info("  %s", key)

    if not args.apply:
        logger.info("")
        logger.info("这是预演。加 --apply 才会真正删除。")
        return 0

    await _apply(report)
    logger.info("")
    logger.info("已删除 %d 个线程的图状态与运行记录。", len(report["purgeable"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="清理已删会话留下的图状态与运行记录")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只报告）")
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=60,
        help="只处理最后活动早于该分钟数的线程，默认 60",
    )
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="把「没有 run 记录」的孤儿线程也纳入清理（默认只报告）",
    )
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
