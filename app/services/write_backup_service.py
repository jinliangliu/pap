# app/services/write_backup_service.py
"""写前备份：一次写操作落地之前，先把将被覆盖的状态原样存下来。

放在服务层而不是网关里，是因为它要碰库、要算租户总量，而网关的热路径
不该塞进这些。网关只负责在最合适的时点调一次 `capture`。

三条边界，缺一条这个功能就会变成「看起来备份了」：

1. **尺寸按序列化后的字节算**，不按对象个数。阈值语义是「这份值不值得存」，
   而值得与否取决于它占多少空间。
2. **存满了就停**，不覆盖最旧的那份。覆盖会让「原件还在」在事后变成假的，
   而人对备份的信任恰恰建立在「它还在」上。
3. **存不下必须说出来**。`skipped` 带回原因，网关把它写进日志与审计；
   静默跳过等于告诉后来的人「这里没有备份，所以大概不重要」。

保留期（`_prune`）与第 2 条不矛盾：它删的是**同一份资源上早已没有回滚价值**的
旧快照（第 6 份以前、或 90 天以前），不是「为了腾地方挤掉最新的那份」。没有
保留期时，总量上限会让备份在某天全体静默停止——`store_full` 每日刷日志，而
真正需要恢复的那一件恰好落在停止之后。

备份失败**不吞**：调用方（网关）根据异常决定是否继续。默认语义是
「备不下来就别写」——写前备份的意义就在于它是写的前置条件。
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select

from app.db.session import async_session_factory
from app.models.write_backup import WriteBackup
from app.services import runtime_settings
from app.tenancy.context import tenant_scope
from app.tools.types import BackupSpec

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

# 保留期。两个数各自回答一个问题，缺一个都会让「留多久」变得不可预期：
#
# - `KEEP_PER_RESOURCE`：同一个资源（一张流程、一个连接器）留最近几份。
#   回滚读的是最新那份，往前第 6 份已经没有人会去翻；而按份数留比按天数留
#   更贴近使用方式——改得勤的那几天不该因为「都在今天」而只剩一份。
# - `MAX_AGE_DAYS`：不管多少份，90 天前的都不再留。它挡的是「一年前那张图
#   还是最新一份」的情形：那时按份数看是合法的，按价值看早就不该在库里。
KEEP_PER_RESOURCE = 5
MAX_AGE_DAYS = 90


@dataclass(frozen=True, slots=True)
class BackupOutcome:
    """一次备份尝试的结论。字段名与日志键一一对应，便于直接下日志。"""

    enabled: bool
    captured: bool
    # over_threshold / store_full / disabled / no_hook
    skipped: str | None
    size_bytes: int
    threshold_mb: int
    max_size_mb: int
    backup_id: str | None = None

    def log_fields(self) -> dict[str, Any]:
        """写进日志的那一组字段。

        `backup_enabled` / `backup_threshold_mb` / `backup_max_size_mb` 三个
        必须同时出现：只看 `backup_enabled=true` 时，人会以为「这次一定备了」，
        而实际上可能是阈值挡掉的。三个一起看，才读得出「开关开着、阈值 100、
        这次没备是因为超了」。
        """
        return {
            "backup_enabled": self.enabled,
            "backup_threshold_mb": self.threshold_mb,
            "backup_max_size_mb": self.max_size_mb,
            "backup_captured": self.captured,
            "backup_skipped": self.skipped,
            "backup_size_bytes": self.size_bytes,
            # 原件在哪一行。没有它，「备过了」只能靠时间戳去 `write_backups`
            # 里碰运气找——而事后要恢复的人手里只有这条审计记录。
            # 决策行写不进这个值（备份发生在它之后），所以只能落在结果事件上。
            "backup_id": self.backup_id,
        }


def measure(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """序列化一次，同时得到「能存进 JSON 列的值」与「它的字节数」。

    两件事必须出自同一次序列化：分开算的话，`default=str` 会把日期变成
    一长串文本，度量与实际存下的内容就不再是同一个东西。
    """
    text = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    return json.loads(text), len(text.encode("utf-8"))


async def _trim_resource(
    session: Any,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
) -> int:
    """把「这一个资源」压回保留份数。插完新行之后调用。

    裁剪分两次跑是有意的：插入**之前**那次是为了腾出空间（总量判据要用清理后的
    用量），插入**之后**这次是为了兑现「每个资源留最近几份」——只在插入前裁，
    稳态会是 N+1 份，而文档写的是 N。

    这次的条件比窗口函数窄：直接按资源取第 N 名之后的行。写操作（尤其要过审批的）
    频次很低，多这一条按资源的删除，比在每次写时重扫全租户便宜。
    """
    older = (
        select(WriteBackup.id)
        .where(
            WriteBackup.tenant_id == tenant_id,
            WriteBackup.resource_type == resource_type,
            WriteBackup.resource_id == resource_id,
        )
        .order_by(WriteBackup.created_at.desc(), WriteBackup.id.desc())
        .offset(KEEP_PER_RESOURCE)
        .subquery()
    )
    result = await session.execute(delete(WriteBackup).where(WriteBackup.id.in_(select(older.c.id))))
    return int(getattr(result, "rowcount", 0) or 0)


async def _prune(session: Any, tenant_id: str) -> int:
    """按保留期删掉没有回滚价值的旧快照，返回删掉的条数。

    在**插入之前**跑：总量判据要用的是「清理之后还剩多少」，否则一个早就该
    清理的租户会一直卡在 `store_full`，而它的备份全是三个月前的。反过来，
    保留期也不能替第 2 条兜底——它永远留着每个资源最新的几份。

    两步删除共用一次事务：中间失败会整批回滚，不会留下「删了一半」的库。
    """
    ranked = (
        select(
            WriteBackup.id.label("id"),
            func.row_number()
            .over(
                partition_by=(WriteBackup.resource_type, WriteBackup.resource_id),
                order_by=(WriteBackup.created_at.desc(), WriteBackup.id.desc()),
            )
            .label("rn"),
        )
        .where(WriteBackup.tenant_id == tenant_id)
        .subquery()
    )

    stale = await session.execute(
        delete(WriteBackup).where(WriteBackup.id.in_(select(ranked.c.id).where(ranked.c.rn > KEEP_PER_RESOURCE)))
    )
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=MAX_AGE_DAYS)
    aged = await session.execute(
        delete(WriteBackup).where(
            WriteBackup.tenant_id == tenant_id,
            WriteBackup.created_at < cutoff,
        )
    )

    removed = int(getattr(stale, "rowcount", 0) or 0) + int(getattr(aged, "rowcount", 0) or 0)
    if removed:
        logger.info(
            "按保留期清理了旧备份。",
            extra={
                "event": "write_backup_pruned",
                "removed": removed,
                "keep_per_resource": KEEP_PER_RESOURCE,
                "max_age_days": MAX_AGE_DAYS,
            },
        )
    return removed


async def capture(
    *,
    tenant_id: str,
    tool_name: str,
    decision: str,
    policy_revision: int,
    spec: BackupSpec,
    thread_id: str | None = None,
    run_id: str | None = None,
    approval_id: str | None = None,
    actor_id: str | None = None,
) -> BackupOutcome:
    """按当前配置尝试备份。**不抛异常**以外的判断：尺寸/总量不足时如实返回。

    调用方拿到 `captured=False` 时要不要继续写，是策略问题，由网关决定；
    这里只负责把事实说清楚。
    """
    enabled = runtime_settings.backup_enabled()
    threshold_mb = runtime_settings.backup_threshold_mb()
    max_mb = runtime_settings.backup_max_size_mb()

    stored, size = measure(spec.payload)

    if not enabled:
        return BackupOutcome(
            enabled=False,
            captured=False,
            skipped="disabled",
            size_bytes=size,
            threshold_mb=threshold_mb,
            max_size_mb=max_mb,
        )

    if size > threshold_mb * _MB:
        logger.info(
            "对象超过备份阈值，本次不备份。",
            extra={
                "event": "write_backup_skipped",
                "reason": "over_threshold",
                "tool_name": tool_name,
                "resource_id": spec.resource_id,
                "size_bytes": size,
                "backup_threshold_mb": threshold_mb,
            },
        )
        return BackupOutcome(
            enabled=True,
            captured=False,
            skipped="over_threshold",
            size_bytes=size,
            threshold_mb=threshold_mb,
            max_size_mb=max_mb,
        )

    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                # 先按保留期裁剪，再判总量：否则一个三个月没清理的租户会永远卡在
                # `store_full`，而它占空间的恰恰全是早该删掉的快照。
                await _prune(session, tenant_id)

                used = await session.scalar(
                    select(func.coalesce(func.sum(WriteBackup.size_bytes), 0)).where(
                        WriteBackup.tenant_id == tenant_id
                    )
                )

                if int(used or 0) + size > max_mb * _MB:
                    logger.warning(
                        "备份总量已达上限，本次不备份。",
                        extra={
                            "event": "write_backup_skipped",
                            "reason": "store_full",
                            "tool_name": tool_name,
                            "resource_id": spec.resource_id,
                            "used_bytes": int(used or 0),
                            "size_bytes": size,
                            "backup_max_size_mb": max_mb,
                        },
                    )
                    return BackupOutcome(
                        enabled=True,
                        captured=False,
                        skipped="store_full",
                        size_bytes=size,
                        threshold_mb=threshold_mb,
                        max_size_mb=max_mb,
                    )

                row = WriteBackup(
                    tenant_id=tenant_id,
                    tool_name=tool_name,
                    resource_type=spec.resource_type,
                    resource_id=spec.resource_id,
                    decision=decision,
                    policy_revision=policy_revision,
                    payload=stored,
                    size_bytes=size,
                    thread_id=thread_id,
                    run_id=run_id,
                    approval_id=approval_id,
                    actor_id=actor_id,
                )
                session.add(row)
                await session.flush()
                backup_id = str(row.id)
                # 插入前那次裁剪留下的是「去掉新行之后的 N 份」，所以这一步才把
                # 这个资源压回 N。两处缺一：只有前者会稳定多留一份，只有后者
                # 则腾不出空间（总量判据用的是插之前的用量）。
                await _trim_resource(session, tenant_id, spec.resource_type, spec.resource_id)

    return BackupOutcome(
        enabled=True,
        captured=True,
        skipped=None,
        size_bytes=size,
        threshold_mb=threshold_mb,
        max_size_mb=max_mb,
        backup_id=backup_id,
    )


async def usage_bytes(tenant_id: str) -> int:
    """该租户已占用的备份总量。给接口与排障用。"""
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            used = await session.scalar(
                select(func.coalesce(func.sum(WriteBackup.size_bytes), 0)).where(
                    WriteBackup.tenant_id == tenant_id
                )
            )
    return int(used or 0)
