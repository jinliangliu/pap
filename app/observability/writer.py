# app/observability/writer.py
"""观测数据的异步写入。

为什么不在请求路径上直接写库：一次对话会产出 10 条左右的 span，
逐条 INSERT 会把观测变成「业务之外的第二份数据库开销」。这里用一个消费者
任务攒批写入，请求侧只做一次 `put_nowait`（微秒级）。

三条硬规则：

1. 队列满 → 丢最旧并计数。宁可丢观测，不可拖慢对话。
2. 写库失败 → 记一次日志、不重试、不抛。表被删了也不能让对话 500。
3. 关停 → 最多等 3 秒把队列排空，不丢最后一批。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
from collections import defaultdict
from typing import Any

from sqlalchemy import update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.observability import ObsSpan, ObsTrace
from app.services import runtime_settings
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

# 一批最多写多少条 trace（每条 trace 自带若干 span）。
_MAX_BATCH = 200
# 攒批最长等多久（秒）。观测不是实时系统，1 秒足够。
_FLUSH_INTERVAL = 1.0
# 关停时等队列排空的上限（秒）。
_DRAIN_TIMEOUT = 3.0

# 队列里的收尾哨兵。用对象身份判断，不会和真实的 bundle（dict）混淆。
_STOP: Any = object()


def _merge_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一批里同一条 trace 只写一次。

    一轮在 1 秒内跑完时，它的 start 与 finish 会落在同一批里。分别写会先 INSERT
    再 UPDATE，既多一次往返，也让别处的查询有机会读到那个中间态（`running`
    而实际已经结束）。合并后只留最终态：后到的 finish 覆盖 start。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in items:
        trace_id = str(item["trace"]["trace_id"])
        if trace_id not in merged:
            merged[trace_id] = dict(item)
            merged[trace_id]["spans"] = list(item.get("spans") or [])
            order.append(trace_id)
            continue

        current = merged[trace_id]
        if item.get("type") == "finish":
            current["type"] = "finish"
            current["trace"] = item["trace"]
        current["spans"].extend(item.get("spans") or [])

    return [merged[trace_id] for trace_id in order]


async def _write_one(session: Any, item: dict[str, Any]) -> None:
    """写一条 trace（起始行或收尾行），必要时带上它的 span。"""
    trace = dict(item["trace"])

    if item.get("type") == "start":
        # 起始行只会来一次，直接 INSERT。重放不该发生：同一 trace 的 start
        # 只在 trace_run 进入时提交一次，队列本身也不复制消息。
        session.add(ObsTrace(**trace))
        return

    # 收尾：先 UPDATE，没命中再 INSERT。
    #
    # 不用 `session.merge()`：它靠主键 `id` 定位，而起始行与收尾行各自生成
    # 不同的 uuid4，merge 永远找不到已存在的那行，只会 INSERT 并撞上
    # `trace_id` 的唯一约束。按业务键（trace_id）更新才是这里要的语义。
    result = await session.execute(
        update(ObsTrace).where(ObsTrace.trace_id == trace["trace_id"]).values(**trace)
    )
    if result.rowcount == 0:
        # 没找到说明起始行没写成功：采样丢弃、队列被挤掉、或进程在起始行
        # 落库前重启。这时补一条完整行，不能因为「本该有起始行」就丢掉收尾。
        session.add(ObsTrace(**trace))

    for span_row in item["spans"]:
        session.add(ObsSpan(**span_row))


class ObsWriter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0
        self._sampled_out = 0
        self._written = 0
        self._failed = 0

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        if self._task is not None:
            return
        if not runtime_settings.obs_enabled():
            # 关闭时也要留一行。验收清单靠这条日志
            # 判断「开关确实生效了」——什么都不打的话，「关了」与「启动日志被
            # 截断」「进程根本没重启」长得一模一样。
            logger.info(
                "观测已关闭，不写 obs_traces / obs_spans。",
                extra={"event": "observability_disabled"},
            )
            return

        self._queue = asyncio.Queue(maxsize=settings.obs_queue_size)
        # **必须给空 context**，不能让它继承调用方的。
        #
        # 运行中开启是从 `PUT /admin/runtime-settings` 里调的，那个请求跑在
        # `platform_admin_tenant_scope("platform")` 下；`create_task` 默认复制
        # 当前 context，消费者任务于是带着 `platform` 出生，之后每批数据写库时
        # `tenant_scope("default")` 会撞上「拒绝在同一执行流内切换租户」，
        # 每条 trace 都只留一行 `obs_write_failed`，接口和界面全都显示正常。
        #
        # 消费者本身不该属于任何租户：它逐批从 bundle 里读 tenant_id 再进作用域。
        self._task = asyncio.create_task(
            self._consume(), name="obs-writer", context=contextvars.Context()
        )
        logger.info(
            "观测已开启。",
            extra={
                "event": "observability_enabled",
                "source": runtime_settings.source(runtime_settings.OBSERVABILITY_ENABLED),
                "capture_content": settings.obs_capture_content,
                "sample_rate": settings.obs_sample_rate,
                "retention_days": settings.obs_retention_days,
            },
        )

        # 生产环境开着观测 = 完整 prompt 与回答落库。提醒保留，但不再要求
        # 「上线前关掉」：关不关由管理员在控制台决定（运行配置 → 观测），
        # 这里只说清代价与去处。
        if settings.is_production:
            logger.warning(
                "生产环境开启了观测：完整 prompt 与回答会写入 obs_traces/obs_spans，"
                "按 OBS_RETENTION_DAYS 清理。如需关闭，走控制台「运行配置 → 观测」，"
                "不需要改 .env、也不需要重启。",
                extra={"event": "observability_enabled_in_production"},
            )

    async def set_enabled(self, enabled: bool) -> None:
        """运行中切换开关。管理员在控制台保存后调用，不重启进程。

        内存里的开关由 `runtime_settings` 负责，这里只管后台任务的启停。
        调用方必须**先**写配置再调本方法：`start()` 会再读一次开关，
        顺序反了会「刚开就判定为关」。
        """
        running = self._task is not None and not self._task.done()

        if enabled:
            if running:
                return
            await self.start()
            return

        if not running:
            # 本来就没在跑（启动时就是关的），也要留一行，否则「管理员刚关了」
            # 与「一直没开过」在日志里分不出来。
            logger.info(
                "观测已关闭，不写 obs_traces / obs_spans。",
                extra={"event": "observability_disabled", "source": "runtime"},
            )
            return

        await self.stop()

    async def stop(self) -> None:
        if self._task is None:
            return

        queue = self._queue
        task = self._task

        if queue is not None:
            # 用哨兵而不是直接 cancel：cancel 会打断正在攒批的消费者，
            # 已经拿在手里的那一批就丢了。塞一个「停」进去，让它自己收尾。
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_STOP)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=_DRAIN_TIMEOUT)

        if not task.done():
            # 兜底：队列满塞不进哨兵、或消费者卡在写库上。
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self._task = None
        self._queue = None

        logger.info(
            "观测已停止。",
            extra={
                "event": "observability_stopped",
                "written": self._written,
                "dropped": self._dropped,
                "failed": self._failed,
            },
        )

    # ---------------- 提交 ----------------

    def submit(self, bundle: dict[str, Any]) -> None:
        """请求侧入口。任何失败都吞掉——不能因为观测把对话带挂。"""
        if not runtime_settings.obs_enabled():
            return

        if not self._keep(bundle):
            self._sampled_out += 1
            return

        queue = self._queue
        if queue is None:
            # 未启动（例如在 CLI 脚本或测试里直接调用）：静默丢弃，不建临时队列。
            return

        try:
            queue.put_nowait(bundle)
        except asyncio.QueueFull:
            # 丢最旧：新的更有可能是正在排查的那一轮。
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                queue.task_done()
            self._dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(bundle)

    def _keep(self, bundle: dict[str, Any]) -> bool:
        """采样只对成功的 trace 生效；错误一律保留。

        起始行（`type="start"`）单独一条规则：**采样率不到 1 就一条都不写**。
        被采掉的那一轮不会再有收尾行，它的起始行会永久停在 `running`——
        「现在谁在跑」这张视图的全部可信度都押在这里，宁可少几行起始记录，
        也不留一个永远不会收尾的假活人。
        """
        rate = settings.obs_sample_rate
        if rate >= 1.0:
            return True
        if bundle.get("type") == "start":
            return False
        if bundle.get("trace", {}).get("status") != "ok":
            return True
        return random.random() < rate

    # ---------------- 消费 ----------------

    async def _consume(self) -> None:
        queue = self._queue
        assert queue is not None

        while True:
            first = await queue.get()
            if first is _STOP:
                queue.task_done()
                return

            batch = [first]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _FLUSH_INTERVAL
            stop_after_flush = False

            while len(batch) < _MAX_BATCH:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    # 只吞超时。CancelledError 必须继续上抛，否则 cancel 之后
                    # 这个循环会自己转回来重新 await，任务永远结束不了。
                    break

                if nxt is _STOP:
                    # 收尾信号：先把手里的这批写完再退出，别丢。
                    queue.task_done()
                    stop_after_flush = True
                    break
                batch.append(nxt)

            try:
                await self._flush(batch)
            finally:
                for _ in batch:
                    queue.task_done()

            if stop_after_flush:
                return

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        # 一个批次可能跨租户（不同请求同时结束），按租户分组写，
        # 否则 RLS 的 WITH CHECK 会把别人的行挡下来。
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in batch:
            groups[item["tenant_id"]].append(item)

        for tenant_id, raw_items in groups.items():
            items = _merge_batch(raw_items)
            try:
                with tenant_scope(tenant_id):
                    async with async_session_factory() as session:
                        async with session.begin():
                            for item in items:
                                await _write_one(session, item)
                self._written += len(items)
            except Exception:
                # 表不存在、磁盘满、RLS 拒绝……都只记一条日志。
                self._failed += len(items)
                logger.warning(
                    "观测数据写入失败，本批丢弃。",
                    extra={
                        "event": "obs_write_failed",
                        "tenant_id": tenant_id,
                        "count": len(items),
                    },
                    exc_info=True,
                )

    # ---------------- 状态 ----------------

    def stats(self) -> dict[str, Any]:
        queue = self._queue
        return {
            "enabled": runtime_settings.obs_enabled(),
            "capture_content": settings.obs_capture_content,
            "sample_rate": settings.obs_sample_rate,
            "retention_days": settings.obs_retention_days,
            "keep_error_days": settings.obs_keep_error_days,
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "sampled_out": self._sampled_out,
            "queue_depth": queue.qsize() if queue is not None else 0,
            "queue_capacity": settings.obs_queue_size if queue is not None else 0,
            "running": self._task is not None and not self._task.done(),
        }


_writer = ObsWriter()


def get_writer() -> ObsWriter:
    return _writer
