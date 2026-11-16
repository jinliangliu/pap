# app/services/obs_sweep.py
"""清扫观测的残行：跑到一半进程没了，留下 `status="running"` 的那一条。

为什么需要这一步：`trace_run` 进入时就落一行 `running`，收尾再改成最终状态。
进程被 kill、服务器重启、OOM——那行就永远停在 `running`。不做清扫，
「现在谁在跑」这张视图会一直把死掉的轮次当成活人，而且越积越多，
最后没人再信它。

**为什么不是删除。** 「有一轮跑到一半进程没了」本身就是需要被看见的事实：
它意味着那一次对话的结果不可知，用户可能什么也没收到。删掉等于把事故抹平，
第二天没人知道昨晚重启过。所以改成 `abandoned` + `error_category="orphaned_running"`，
让它留在列表里，能查、能数、能对上重启时间。

`ended_at` / `duration_ms` 故意留空：不知道它什么时候停的，编一个时间会让
「耗时」这个数凭空多出一段。界面上显示为「未知」，比一个假的数字诚实。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.observability import ObsTrace
from app.services import runtime_settings

logger = logging.getLogger(__name__)

# 判为残行的状态词。
ORPHAN_STATUS = "abandoned"
ORPHAN_CATEGORY = "orphaned_running"


async def sweep_orphaned_runs(
    *,
    ttl_seconds: int | None = None,
    limit: int = 500,
) -> int:
    """把超时未收尾的 trace 改成 abandoned，返回处理条数。

    调用方负责设好租户上下文（维护任务是逐租户遍历的）。
    """
    if not runtime_settings.obs_enabled():
        # 观测关掉时这张表可能根本不存在。不开观测还去扫，等于每次维护都报一次错。
        return 0

    ttl = ttl_seconds if ttl_seconds is not None else settings.obs_running_ttl_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl)

    try:
        async with async_session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(ObsTrace)
                        .where(ObsTrace.status == "running")
                        .where(ObsTrace.started_at < cutoff)
                        .limit(limit)
                    )
                ).all()

                for row in rows:
                    row.status = ORPHAN_STATUS
                    row.error_category = ORPHAN_CATEGORY
                    # 不改 ended_at / duration_ms，理由见模块说明。
    except Exception:
        # 清扫失败不能把整轮维护带停（同一事务里还有审批过期、
        # 卡死执行恢复要跑）。记一条，下一轮再试。
        logger.warning(
            "残行清扫失败，本轮跳过。",
            extra={"event": "obs_sweep_failed"},
            exc_info=True,
        )
        return 0

    if rows:
        logger.info(
            "观测残行已标记。",
            extra={"event": "obs_orphans_swept", "count": len(rows), "ttl_seconds": ttl},
        )

    return len(rows)
