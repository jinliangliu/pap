# app/api/routes/observability.py
"""观测查询接口：概览、trace 列表、trace 详情（含技能命中与分阶段明细）。

边界，先说清楚：

- **只读**。观测数据由 `ObsWriter` 在对话链路里写，接口不提供任何写入口。
- **不做跨租户聚合**。所有查询都落在 `identity.tenant_id` 内；平台管理员在这里
  也只读自己当前所在租户，跨租户排查走审计页。
- **默认只看自己**。没有 `observability:read` 的成员只能读到 `user_id` 是自己的
  trace——别人问过什么、工具返回了什么，跟他的排障无关。
- **原文只在详情返回**。列表接口绝不带 `input_text` / `output_text`：
  一轮对话的 prompt 动辄几万字符，列表一页 50 条就是几 MB。
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent import Agent
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.observability import ObsSpan, ObsTrace
from app.models.workflow import Workflow
from app.orchestration.definition import (
    DefinitionError,
    agent_edges,
    compile_definition,
)
from app.models.skill import Skill, SkillHit
from app.observability.state import display_state, needs_attention
from app.schemas.common import as_utc
from app.services import runtime_settings
from app.schemas.observability import (
    ObsAgentBaselineResponse,
    ObsAgentBucket,
    ObsAgentStat,
    ObsBucket,
    ObsChangeReason,
    ObsDeclaredEdge,
    ObsLiveResponse,
    ObsLiveRun,
    ObsLiveWaiting,
    ObsOperationApproval,
    ObsOperationDelegation,
    ObsOperationParticipant,
    ObsOperationResponse,
    ObsOperationStep,
    ObsOperationTrace,
    ObsOverviewBucket,
    ObsOverviewResponse,
    ObsSkillInsight,
    ObsSpanItem,
    ObsToolStat,
    ObsTopologyEdge,
    ObsTopologyNode,
    ObsTopologyResponse,
    ObsTraceDetailResponse,
    ObsTraceListResponse,
    ObsTraceSummary,
    ObsWindow,
)
from app.services import skill_service
from app.tenancy.context import tenant_scope

router = APIRouter(prefix="/observability", tags=["observability"])

# 能看整个租户的权限点。生产环境给某个角色加这个权限只需要往
# role_permissions 里插一行。
READ_PERMISSION = "observability:read"

MAX_WINDOW_HOURS = 24 * 14


def _scope_of(identity: Identity) -> str:
    if identity.is_platform_admin or READ_PERMISSION in identity.scopes:
        return "tenant"
    return "self"


# 没有结论的状态：既不是成功也不是失败，一律不计入错误率。
#
# - `running`：还在跑。算进去的话，每次有人点发送错误率就跳一下——
#   「刚发一句话就看见系统变差」是最容易让人不再相信这个数字的方式。
# - `awaiting_approval`：停在等人确认。这是**设计好的暂停**，不是事故：
#   单子挂在审批页上，确认之后由**另一条** trace 接着执行（同一个
#   operation_id）。把它算成错误，等于给每一次
#   正常的写操作记一笔失败——实测（2026-11-12）让错误率长期虚高到三成以上。
CONCLUSIONLESS_STATUSES = frozenset({"running", "awaiting_approval"})


def _is_error(status: str | None) -> bool:
    """这一轮算不算错误。

    判据是「系统有没有做到」，不是「结果是不是成功」。所以：

    - `running` / `awaiting_approval` 不算，理由见上面那个集合；
    - 空值按 `ok` 处理（存量行没有这一列）；
    - `abandoned` 算：跑到一半进程没了，结果不可知，是事故；
    - `cancelled` 也算：用户按下取消时，我们分不清「他改主意了」还是
      「这一轮卡到他放弃」。后者是真缺陷，宁可让它留在错误率里。
    - 不认识的状态一律算错误。**未知不等于健康**：真出了新的失败类型，
      数字应该往上跳一下引起注意，而不是被静默忽略。

    改口径的代价写在脸上：新旧截图上的数字对不上。原始分布 `by_status`
    一直在返回，按老口径复算随时可以。
    """
    value = status or "ok"
    return value != "ok" and value not in CONCLUSIONLESS_STATUSES


def _scoped(stmt: Select, *, identity: Identity, scope: str) -> Select:
    """统一的可见范围收敛。少这个条件就是越权。"""
    stmt = stmt.where(ObsTrace.tenant_id == identity.tenant_id)
    if scope == "self":
        stmt = stmt.where(ObsTrace.user_id == identity.user_id)
    return stmt


def _summary(row: ObsTrace) -> ObsTraceSummary:
    # trace 自己的 `awaiting_approval` 就是「有一张 pending 单子」的等价信息，
    # 列表页据此派生状态，不必为每一行回查审批表（那会变成 N+1 次查询）。
    pending = "PENDING" if row.status == "awaiting_approval" else None
    return ObsTraceSummary(
        trace_id=row.trace_id,
        operation_id=row.operation_id or row.trace_id,
        display_state=display_state(
            trace_status=row.status,
            approval_status=pending,
            error_category=row.error_category,
        ),
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        agent_name=row.agent_name,
        started_at=row.started_at,
        duration_ms=row.duration_ms,
        status=row.status,
        error_category=row.error_category,
        intent=row.intent,
        tool_name=row.tool_name,
        skill_name=row.skill_name,
        span_count=row.span_count or 0,
        llm_calls=row.llm_calls or 0,
        tool_calls=row.tool_calls or 0,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cost_micros=row.cost_micros,
    )


def _window(
    *, hours: int, from_ts: datetime | None, to_ts: datetime | None
) -> tuple[datetime, datetime]:
    """时间窗。显式 from/to 优先于 hours，两者都给也以 from/to 为准。"""
    end = to_ts or datetime.now(UTC)
    start = from_ts or (end - timedelta(hours=hours))
    if start > end:
        raise HTTPException(status_code=400, detail="起始时间不能晚于结束时间。")
    return start, end


def _pct(values: list[int], q: float) -> int | None:
    """最近秩法取分位：小样本时比插值更好解释（14 条里取第 14 条，而不是第 13.3 条）。"""
    if not values:
        return None
    idx = min(len(values) - 1, max(0, int(round(q * len(values))) - 1))
    return values[idx]


# 趋势分桶的候选桶宽（小时）。取第一个能让格数不超过上限的：
# 28 格是折线在表格行里还看得清的上限，再多就退化成一条噪声带。
# 最长窗口 14 天落在 12h 这一档（28 格）；表末尾是兜底，只有把窗口上限
# 继续放大时才会用到。
BUCKET_LADDER = (1, 3, 6, 12)
MAX_BUCKETS = 28

# 少于这个轮次就不算比例。这不是显著性检验，只是拦住最离谱的读数：
# 2 轮 → 3 轮是「涨 50%」，实际是同一个人的两次重试。
TREND_MIN_SAMPLE = 5


def _pick_bucket_hours(hours: int) -> int:
    for candidate in BUCKET_LADDER:
        if hours / candidate <= MAX_BUCKETS:
            return candidate
    return BUCKET_LADDER[-1]


def _bucket_edges(*, start: datetime, end: datetime, bucket_hours: int) -> list[datetime]:
    """每一格的**结束**时刻，从最旧排到最新。

    从窗口末尾往前切，所以最后一格正好收在 `end`（= 现在），已经走完；
    只有最旧一格可能被窗口起点截断。按自然日对齐会让首尾同时残缺，
    且把日历语义混进滚动窗口——「周一早上看什么都像暴跌」就是这么来的。
    """
    width = bucket_hours * 3600
    count = max(1, math.ceil((end - start).total_seconds() / width))
    return [end - timedelta(seconds=(count - k - 1) * width) for k in range(count)]


def _bucket_index(*, ts: datetime, end: datetime, bucket_hours: int) -> int:
    """距今第几格（0 = 最新）。超出窗口的返回负数，调用方自行丢弃。"""
    return int((end.timestamp() - as_utc(ts).timestamp()) // (bucket_hours * 3600))


def _change(
    *, current: int, previous: int, created_at: datetime | None, prev_start: datetime
) -> tuple[float | None, ObsChangeReason | None]:
    """相对变化，以及算不出比例时的原因。

    `None` + 原因 而不是 0：把「没法比」画成「持平」正是这类报表最容易骗人的地方。
    """
    # 上一窗口开始时这个 Agent 还不存在（或刚建），那一段的覆盖不完整，
    # 拿它当基线等于拿半天比七天。比 `no_baseline` 更靠前判断：新建的 Agent
    # 上一窗口通常正好是 0，只说「没基线」就分不清它是刚起步还是被放弃了。
    if created_at is not None and as_utc(created_at) > prev_start:
        return None, "created_after_baseline_start"

    if previous == 0:
        # 除不出比例。也不再编一个「从 0 到 N」当作 +100%。
        return None, "no_baseline"

    if previous < TREND_MIN_SAMPLE or current < TREND_MIN_SAMPLE:
        return None, "low_sample"

    return round((current - previous) / previous, 4), None


def _overview_buckets(
    rows: list[ObsTrace], *, start: datetime, end: datetime, hours: int
) -> tuple[list[ObsOverviewBucket], int]:
    """把窗口切成等宽时间格，返回 `(buckets, bucket_hours)`。

    切格复用 `/agents` 那三个函数（`_pick_bucket_hours` / `_bucket_edges` /
    `_bucket_index`）而不是另写一套：同一页上两张走势图必须能并排读，
    各自切一套格子，格与格就对不上了。

    `awaiting` 单独计数而不是并进 `errors` 或「其余」：它是设计好的暂停
    （见 `CONCLUSIONLESS_STATUSES`），在堆叠柱里既不属于「做成了」也不属于
    「出错了」，混进任一侧都会让那一层颜色说谎。
    """
    bucket_hours = _pick_bucket_hours(hours)
    edges = _bucket_edges(start=start, end=end, bucket_hours=bucket_hours)
    width = bucket_hours * 3600
    oldest_partial = (edges[0] - start).total_seconds() < width

    traces = [0] * len(edges)
    errors = [0] * len(edges)
    awaiting = [0] * len(edges)
    durations: list[list[int]] = [[] for _ in edges]

    for row in rows:
        if row.started_at is None:
            continue
        # 0 = 最新。落在窗口外的丢弃：窗口边界与格边界不整除时，
        # 最旧那一格会有一小截落在窗口外（与 `/agents` 同一处理）。
        idx = _bucket_index(ts=row.started_at, end=end, bucket_hours=bucket_hours)
        if not 0 <= idx < len(edges):
            continue
        slot = len(edges) - 1 - idx
        traces[slot] += 1
        if _is_error(row.status):
            errors[slot] += 1
        if row.status == "awaiting_approval":
            awaiting[slot] += 1
        if row.duration_ms is not None:
            durations[slot].append(int(row.duration_ms))

    buckets = [
        ObsOverviewBucket(
            t=edge,
            traces=traces[i],
            errors=errors[i],
            awaiting=awaiting[i],
            p95_duration_ms=_pct(sorted(durations[i]), 0.95),
            partial=oldest_partial and i == 0,
        )
        for i, edge in enumerate(edges)
    ]
    return buckets, bucket_hours


@router.get("/overview", response_model=ObsOverviewResponse)
async def get_overview(
    hours: int = Query(default=24, ge=1, le=MAX_WINDOW_HOURS),
    identity: Identity = Depends(get_current_identity),
) -> ObsOverviewResponse:
    """窗口内的总量、错误率、耗时分布与工具排行。

    p95 拿 Python 算而不是 `percentile_cont`：观测表行数是「14 天 × 对话轮次」，
    抽一列 `duration_ms` 回来的代价远小于为此把 SQLite 测试环境排除在外。
    """
    scope = _scope_of(identity)
    start, end = _window(hours=hours, from_ts=None, to_ts=None)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            base = _scoped(
                select(ObsTrace).where(
                    ObsTrace.started_at >= start,
                    ObsTrace.started_at < end,
                ),
                identity=identity,
                scope=scope,
            )

            rows = (await session.scalars(base)).all()

            status_rows = (
                await session.execute(
                    _scoped(
                        select(ObsTrace.status, func.count(ObsTrace.id)).where(
                            ObsTrace.started_at >= start,
                            ObsTrace.started_at < end,
                        ),
                        identity=identity,
                        scope=scope,
                    ).group_by(ObsTrace.status)
                )
            ).all()

            intent_rows = (
                await session.execute(
                    _scoped(
                        select(ObsTrace.intent, func.count(ObsTrace.id)).where(
                            ObsTrace.started_at >= start,
                            ObsTrace.started_at < end,
                        ),
                        identity=identity,
                        scope=scope,
                    )
                    .group_by(ObsTrace.intent)
                    .order_by(func.count(ObsTrace.id).desc())
                )
            ).all()

            skill_rows = (
                await session.execute(
                    _scoped(
                        select(ObsTrace.skill_name, func.count(ObsTrace.id)).where(
                            ObsTrace.started_at >= start,
                            ObsTrace.started_at < end,
                            ObsTrace.skill_name.is_not(None),
                        ),
                        identity=identity,
                        scope=scope,
                    )
                    .group_by(ObsTrace.skill_name)
                    .order_by(func.count(ObsTrace.id).desc())
                )
            ).all()

            tool_rows = (
                await session.execute(
                    _scoped(
                        select(
                            ObsTrace.tool_name,
                            func.count(ObsTrace.id),
                            func.sum(func.coalesce(ObsTrace.duration_ms, 0)),
                            # 出错次数。判定与 Python 侧 `_is_error` 保持同一套：
                            # 空值当 ok，`running` / `awaiting_approval` 不算错，
                            # 不认识的取值算错。两处如果分歧，图上会画出一个
                            # 与错误率对不上的红条，而那种不一致最难解释。
                            func.sum(
                                case(
                                    (
                                        and_(
                                            func.coalesce(ObsTrace.status, "ok") != "ok",
                                            func.coalesce(ObsTrace.status, "ok").notin_(
                                                tuple(CONCLUSIONLESS_STATUSES)
                                            ),
                                        ),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                        ).where(
                            ObsTrace.started_at >= start,
                            ObsTrace.started_at < end,
                            ObsTrace.tool_name.is_not(None),
                        ),
                        identity=identity,
                        scope=scope,
                    )
                    .group_by(ObsTrace.tool_name)
                    .order_by(func.count(ObsTrace.id).desc())
                    .limit(8)
                )
            ).all()

    durations = sorted(int(r.duration_ms) for r in rows if r.duration_ms is not None)
    error_rows = [r for r in rows if _is_error(r.status)]
    agent_counts = Counter(r.agent_name if r.agent_name else "—" for r in rows)
    status_counts = {str(k or "unknown"): int(c) for k, c in status_rows}
    buckets, bucket_hours = _overview_buckets(rows, start=start, end=end, hours=hours)

    return ObsOverviewResponse(
        hours=hours,
        enabled=runtime_settings.obs_enabled(),
        traces=len(rows),
        errors=len(error_rows),
        error_rate=round(len(error_rows) / len(rows), 4) if rows else 0.0,
        # 不计入错误率，但绝不能凭空消失：这批轮次停在等人确认，
        # 界面上要能回答「那 20 轮去哪了」。
        awaiting_approval=status_counts.get("awaiting_approval", 0),
        llm_calls=sum(int(r.llm_calls or 0) for r in rows),
        tool_calls=sum(int(r.tool_calls or 0) for r in rows),
        prompt_tokens=sum(int(r.prompt_tokens or 0) for r in rows),
        completion_tokens=sum(int(r.completion_tokens or 0) for r in rows),
        avg_duration_ms=round(sum(durations) / len(durations)) if durations else None,
        p95_duration_ms=_pct(durations, 0.95),
        max_duration_ms=durations[-1] if durations else None,
        by_status=[ObsBucket(key=k, count=v) for k, v in status_counts.items()],
        by_intent=[ObsBucket(key=str(k or "—"), count=int(c)) for k, c in intent_rows],
        by_skill=[ObsBucket(key=str(k), count=int(c)) for k, c in skill_rows],
        by_agent=[
            ObsBucket(key=str(k), count=int(c)) for k, c in agent_counts.most_common()
        ],
        top_tools=[
            ObsToolStat(
                tool_name=str(name),
                calls=int(count),
                errors=int(err_count or 0),
                avg_duration_ms=int(total // count) if count else None,
            )
            for name, count, total, err_count in tool_rows
        ],
        buckets=buckets,
        bucket_hours=bucket_hours,
        last_error_at=max((r.started_at for r in error_rows if r.started_at), default=None),
        scope=scope,
    )


@router.get("/agents", response_model=ObsAgentBaselineResponse)
async def get_agent_baseline(
    hours: int = Query(default=168, ge=1, le=MAX_WINDOW_HOURS),
    limit: int = Query(default=50, ge=1, le=200),
    compare: bool = Query(default=True, description="是否与上一个等长窗口对比"),
    bucket_hours: int | None = Query(
        default=None, ge=1, le=24, description="趋势桶宽；不传按窗口长度自动选"
    ),
    identity: Identity = Depends(get_current_identity),
) -> ObsAgentBaselineResponse:
    """按智能体的运算基准：谁在被用、谁在出错、被挡了几次、花了多少。

    默认窗口 168 小时而不是 24 小时：一天的样本量不足以判断「这个 Agent 是不是
    只有一个人在试」，而按 Agent 做取舍（拆不拆分工、要不要加配额）看的正是周级
    的量。

    截面之外还带趋势：同一等长的上一窗口作基线，加一条分格折线。两个窗口都是
    **滚动**的（都止于此刻、等长），不是自然周——自然周会让「周一早上」这类
    时刻永远显得暴跌，而那只是窗口口径的问题，不是用量的问题。

    治理侧的三个数（被拒 / 审批 / 驳回）不是从 trace 推出来的——拒绝发生在执行
    **之前**，没有 trace 行可归。所以按 `agent_name` 快照从审计与审批表单独统计，
    再与 trace 侧按名字对齐。名字都取写入时的快照：Agent 改名后不必把历史改写
    成新名字，那会让「上个月它跑成什么样」失真。

    审计侧没有 `agent_name` 的记录（不经对话链路的直接工具调用）不计入任何一行。

    `delegated_turns` 是另一条来源：`kind=agent` 的 span。委派的子调用跑在
    发起方那条 trace 里，所以「被叫去几次」与「自己跑了几轮」是两个数，
    不能相加——一个 Agent 可以 0 轮自己的对话、却被叫了 20 次。
    """
    scope = _scope_of(identity)
    start, end = _window(hours=hours, from_ts=None, to_ts=None)
    in_window = (ObsTrace.started_at >= start, ObsTrace.started_at < end)

    # 上一窗口与当前窗口等长、紧邻、同样止于一个确定时刻——两段的「完整度」
    # 因此一致，比例才有意义。
    span = end - start
    prev_end = start
    prev_start = start - span
    if bucket_hours is None:
        bucket_hours = _pick_bucket_hours(hours)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            # 只取统计列。`select(ObsTrace)` 会把 input_text / output_text 一起捞回来
            # （单条可达几十 KB），而这里默认窗口是 168 小时——一周的原文没有一处用得上。
            rows = (
                await session.execute(
                    _scoped(
                        select(
                            ObsTrace.agent_name,
                            ObsTrace.status,
                            ObsTrace.duration_ms,
                            ObsTrace.llm_calls,
                            ObsTrace.tool_calls,
                            ObsTrace.prompt_tokens,
                            ObsTrace.completion_tokens,
                            ObsTrace.cost_micros,
                            ObsTrace.started_at,
                        ).where(*in_window),
                        identity=identity,
                        scope=scope,
                    )
                )
            ).all()

            # 定义侧：这些 Agent 现在还在不在、叫什么、哪一版。只用于显示，
            # 不参与统计——否则删掉一个 Agent，它的历史用量就跟着消失了。
            # `created_at` 是给趋势用的：上一窗口开始时它还不存在，那一段的
            # 覆盖就不完整，不能拿来当基线。
            agent_rows = (
                await session.execute(
                    select(
                        Agent.name,
                        Agent.display_name,
                        Agent.status,
                        Agent.revision,
                        Agent.created_at,
                    ).where(Agent.tenant_id == identity.tenant_id)
                )
            ).all()

            # 上一等长窗口，只数轮次：趋势问的是「有没有在被用」，
            # 而不是「token 涨没涨」（token 有空值，算比例会凭空多出涨幅）。
            previous_rows = []
            if compare:
                previous_rows = (
                    await session.execute(
                        _scoped(
                            select(ObsTrace.agent_name, func.count(ObsTrace.id)).where(
                                ObsTrace.started_at >= prev_start,
                                ObsTrace.started_at < prev_end,
                            ),
                            identity=identity,
                            scope=scope,
                        ).group_by(ObsTrace.agent_name)
                    )
                ).all()

            approval_stmt = (
                select(
                    ApprovalRequest.agent_name,
                    ApprovalRequest.status,
                    func.count(ApprovalRequest.id),
                )
                .where(
                    ApprovalRequest.tenant_id == identity.tenant_id,
                    ApprovalRequest.created_at >= start,
                    ApprovalRequest.created_at < end,
                )
                .group_by(ApprovalRequest.agent_name, ApprovalRequest.status)
            )
            if scope == "self":
                approval_stmt = approval_stmt.where(ApprovalRequest.user_id == identity.user_id)
            approval_rows = (await session.execute(approval_stmt)).all()

            # 只把 JSON 列取回来在 Python 里数：`->>` 与 `json_extract` 的写法
            # 不通用，而这个表按事件类型筛过之后行数很小。`created_at` 一起取，
            # 用来把拒绝也放进趋势格——「用量没涨、被拒在涨」正是边界配窄的信号。
            denial_stmt = select(AuditLog.metadata_json, AuditLog.created_at).where(
                AuditLog.tenant_id == identity.tenant_id,
                AuditLog.event_type == AuditEventType.TOOL_DENIED,
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
            )
            if scope == "self":
                denial_stmt = denial_stmt.where(AuditLog.actor_id == identity.user_id)
            denial_payloads = (await session.execute(denial_stmt)).all()

            # 被委派次数。`kind=agent` 的 span 只有委派会写（`delegate_tools`
            # 里那一层 `agent.{target}`），所以一个 span = 一次「被叫去干活」。
            #
            # join trace 而不是直接按 span 过滤：span 上没有 `user_id`，
            # 「只看自己」这个范围只能从 trace 的主人来定——而委派这件事
            # 该记在提要求的人账上，不是被叫去的那个人。
            delegated_stmt = _scoped(
                select(ObsSpan.agent_name, func.count(ObsSpan.id))
                .join(ObsTrace, ObsTrace.trace_id == ObsSpan.trace_id)
                .where(
                    ObsSpan.kind == "agent",
                    ObsSpan.started_at >= start,
                    ObsSpan.started_at < end,
                )
                .group_by(ObsSpan.agent_name),
                identity=identity,
                scope=scope,
            )
            delegated_rows = (await session.execute(delegated_stmt)).all()

            # 等待时长要按「区间与窗口有交集」取，不能按「单子创建在窗口内」：
            # 一张昨天开、至今没人处理的单子，今天这段时间也一直有人在等。
            # 只取参与判定的几列——`reason` / `parameters_json` 一个字都用不上。
            resolved_at = func.coalesce(ApprovalRequest.approved_at, ApprovalRequest.completed_at)
            wait_stmt = select(
                ApprovalRequest.agent_name,
                ApprovalRequest.created_at,
                ApprovalRequest.approved_at,
                ApprovalRequest.completed_at,
                ApprovalRequest.expires_at,
                ApprovalRequest.status,
            ).where(
                ApprovalRequest.tenant_id == identity.tenant_id,
                ApprovalRequest.created_at < end,
                or_(resolved_at.is_(None), resolved_at > start),
            )
            if scope == "self":
                wait_stmt = wait_stmt.where(ApprovalRequest.user_id == identity.user_id)
            wait_rows = (await session.execute(wait_stmt)).all()

            # 它把活转交给了谁。读审计而不是 span：被拒的转交没有 span，
            # 而「一直在被拒」恰恰是这个 Agent 边界配窄了的证据。
            delegation_stmt = select(AuditLog.metadata_json).where(
                AuditLog.tenant_id == identity.tenant_id,
                AuditLog.event_type == AuditEventType.AGENT_DELEGATED,
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
            )
            if scope == "self":
                delegation_stmt = delegation_stmt.where(AuditLog.actor_id == identity.user_id)
            delegation_rows = (await session.execute(delegation_stmt)).all()

    traces_by_agent: dict[str, list[ObsTrace]] = {}
    unattributed = 0
    for row in rows:
        if not row.agent_name:
            unattributed += 1
            continue
        traces_by_agent.setdefault(row.agent_name, []).append(row)

    meta = {
        name: (display, status_value, revision, created_at)
        for name, display, status_value, revision, created_at in agent_rows
    }

    approvals: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for name, status_value, count in approval_rows:
        if not name:
            continue
        approvals[name] += int(count)
        # 枚举成员与字符串都当字符串比：SQLite 上取回的是成员，PG 上取决于驱动。
        if str(getattr(status_value, "value", status_value)) == ApprovalStatus.REJECTED.value:
            rejected[name] += int(count)

    delegated: Counter[str] = Counter()
    for name, count in delegated_rows:
        if name:
            delegated[str(name)] = int(count)

    # 等待时长：先按 Agent 攒区间，再取并集。裁剪到窗口是必须的——
    # 一张横跨窗口边界的单子只该贡献落在窗口里的那一段。
    waiting: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in wait_rows:
        if not row.agent_name:
            continue
        wait_start, wait_end = _approval_wait(row, end)
        clipped_start = max(wait_start, start)
        clipped_end = min(wait_end, end)
        if clipped_end > clipped_start:
            waiting.setdefault(str(row.agent_name), []).append((clipped_start, clipped_end))

    delegated_to: dict[str, Counter[str]] = {}
    for (payload,) in delegation_rows:
        meta = payload if isinstance(payload, dict) else {}
        source = meta.get("from_agent")
        target = meta.get("to_agent")
        if source and target:
            delegated_to.setdefault(str(source), Counter())[str(target)] += 1

    denials: Counter[str] = Counter()
    denial_times: dict[str, list[datetime]] = {}
    for payload, created_ts in denial_payloads:
        name = (payload or {}).get("agent_name")
        if not name:
            continue
        denials[str(name)] += 1
        if created_ts is not None:
            denial_times.setdefault(str(name), []).append(as_utc(created_ts))

    previous_counts: dict[str, int] = {}
    for name, count in previous_rows:
        if name:
            previous_counts[str(name)] = int(count)

    edges = _bucket_edges(start=start, end=end, bucket_hours=bucket_hours)
    width = bucket_hours * 3600
    # 只有最旧一格可能被窗口起点截断；其余各格都等长且已走完。
    oldest_partial = (edges[0] - start).total_seconds() < width

    # 名字取三处并集：只在审批或拒绝里出现、窗口内没有 trace 的 Agent 也要露头，
    # 「被拒 5 次却 0 轮对话」正是需要被看见的那种异常。
    # 被委派过的名字也要进来：它可能一次都没有自己的轮次（活儿都在别人的
    # trace 里干），而那正是「谁在被使唤」要回答的那个人。
    names = (
        set(traces_by_agent)
        | {n for n in approvals if n}
        | {n for n in denials if n}
        | {n for n in delegated if n}
        | set(delegated_to)
        | set(waiting)
    )
    total = len(rows)

    stats: list[ObsAgentStat] = []
    for name in names:
        group = traces_by_agent.get(name, [])
        display, status_value, revision, created_at = meta.get(name, (None, None, None, None))
        durations = sorted(int(r.duration_ms) for r in group if r.duration_ms is not None)
        costs = [int(r.cost_micros) for r in group if r.cost_micros is not None]
        errors = sum(1 for r in group if _is_error(r.status))
        # 单独数一遍，而不是从 errors 里减：这两个数的分母不同
        # （错误率的分母是全部轮次，等人工只是其中一部分），
        # 相互推导会在窗口边界上算错。
        awaiting = sum(1 for r in group if r.status == "awaiting_approval")

        bucket_traces = [0] * len(edges)
        bucket_errors = [0] * len(edges)
        bucket_denials = [0] * len(edges)

        def _slot(ts: datetime) -> int | None:
            # 0 = 最新。倒着映射到 edges 的升序下标。落在窗口外的丢弃：
            # 窗口边界与桶边界不整除时，最旧那一格会有一小截落在窗口之外。
            idx = _bucket_index(ts=ts, end=end, bucket_hours=bucket_hours)
            return len(edges) - 1 - idx if 0 <= idx < len(edges) else None

        for r in group:
            slot = _slot(r.started_at) if r.started_at else None
            if slot is None:
                continue
            bucket_traces[slot] += 1
            if _is_error(r.status):
                bucket_errors[slot] += 1

        for ts in denial_times.get(name, []):
            slot = _slot(ts)
            if slot is not None:
                bucket_denials[slot] += 1

        change, reason = (None, None)
        if compare:
            change, reason = _change(
                current=len(group),
                previous=previous_counts.get(name, 0),
                created_at=created_at,
                prev_start=prev_start,
            )

        stats.append(
            ObsAgentStat(
                agent_name=name,
                display_name=display,
                status=getattr(status_value, "value", status_value),
                revision=revision,
                traces=len(group),
                delegated_turns=delegated.get(name, 0),
                delegated_to=[
                    ObsBucket(key=target, count=count)
                    for target, count in delegated_to.get(name, Counter()).most_common(5)
                ],
                waiting_ms=_merge_ms(waiting.get(name, [])),
                errors=errors,
                error_rate=round(errors / len(group), 4) if group else 0.0,
                awaiting_approval=awaiting,
                share=round(len(group) / total, 4) if total else 0.0,
                llm_calls=sum(int(r.llm_calls or 0) for r in group),
                tool_calls=sum(int(r.tool_calls or 0) for r in group),
                prompt_tokens=sum(int(r.prompt_tokens or 0) for r in group),
                completion_tokens=sum(int(r.completion_tokens or 0) for r in group),
                # 一条都没报单价就是 None：0 会被当成「免费」。
                cost_micros=sum(costs) if costs else None,
                avg_duration_ms=round(sum(durations) / len(durations)) if durations else None,
                p95_duration_ms=_pct(durations, 0.95),
                denials=denials.get(name, 0),
                approvals=approvals.get(name, 0),
                approvals_rejected=rejected.get(name, 0),
                last_started_at=max(
                    (r.started_at for r in group if r.started_at), default=None
                ),
                previous_traces=previous_counts.get(name, 0),
                change=change,
                change_reason=reason,
                buckets=[
                    ObsAgentBucket(
                        t=t,
                        traces=bucket_traces[i],
                        errors=bucket_errors[i],
                        denials=bucket_denials[i],
                        partial=oldest_partial and i == 0,
                    )
                    for i, t in enumerate(edges)
                ],
            )
        )

    stats.sort(
        key=lambda s: (
            -s.traces,
            -(s.approvals + s.denials),
            -s.delegated_turns,
            s.agent_name,
        )
    )

    return ObsAgentBaselineResponse(
        hours=hours,
        enabled=runtime_settings.obs_enabled(),
        total_traces=total,
        unattributed_traces=unattributed,
        agents=stats[:limit],
        scope=scope,
        window=ObsWindow(start=start, end=end),
        previous=ObsWindow(start=prev_start, end=prev_end) if compare else None,
        bucket_hours=bucket_hours,
    )


# ---------------------------------------------------------------- L2 委派拓扑

# 审计侧 `status` → 边的四种结局。取值来自 `delegate_tools._audit`，
# 那是每次转交尝试唯一的权威记录。
EDGE_KIND_BY_STATUS = {
    "completed": "ok",
    "awaiting_approval": "paused",
    "rejected": "rejected",
    "failed": "failed",
}

# 被拒原因的展示上限。「一直被 delegate_widens 挡」是结论，
# 后面再挂二十种罕见原因只会把结论埋掉。
TOPOLOGY_MAX_REASONS = 3


@router.get("/topology", response_model=ObsTopologyResponse)
async def get_topology(
    hours: int = Query(default=168, ge=1, le=MAX_WINDOW_HOURS),
    identity: Identity = Depends(get_current_identity),
) -> ObsTopologyResponse:
    """智能体之间**实际发生**的委派关系。

    默认窗口 168 小时：拓扑回答的是「架构长什么样、边界配得对不对」，
    那是周级问题。一天的样本里一次转交都没有，图就是空的。

    ## 边来自审计，不是 span

    原方案打算从 `kind="agent"` 的 span 推边，
    实现时发现两处硬伤：

    1. **被拒的转交根本没有 span。** 深度 / 环 / 预算 / 边界 / 引用五种拒绝
       全部发生在 `agent.{target}` 这个 span 创建**之前**（见
       `delegate_tools.execute`），于是「一直被拒」这条最需要被看见的边在图上
       是空的——而它恰恰是「边界配窄了还是职责写宽了」的唯一证据。
    2. **暂停的转交在 span 上写着 completed。** 子图停在审批时 `ainvoke`
       正常返回、`state["error_category"]` 为空，于是 `handle.add(status=...)`
       记下 `completed`；而 `SpanHandle.add` 写的是 **attrs**，连 span 自己的
       `status` 列都没动。「等人工」和「跑完了」在 span 上长得一模一样。

    审计侧一条 `agent_delegated` 就是一次转交尝试，`metadata.status` 明确是
    `completed / awaiting_approval / rejected / failed` 之一，四种结局可以分开；
    而且它不受观测保留期影响——14 天后 trace 没了，这条关系还在，
    这才是「周级问题」该有的数据寿命。

    span 只用来补一个数：`ok` 边的 P95 墙钟（审计不记耗时）。

    ## 两层：实测 + 声明

    节点 = 观测到的 Agent ∪ 已配置的 Agent。后者里窗口内零活动的那些
    `configured=True, observed=False`，前端灰显、不带边——「建了没人用」
    比图本身更有价值。

    `declared_edges` 来自编排配置（`workflows.definition`），是「应该是这样」；
    `edges` 来自审计，是「实际这样」。两层可以互相对账，两个方向都有信息量：

    - **声明了但从没跑过** → 配了没人用，或者入口藏得太深（`observed=false`）；
    - **跑过但从没声明** → 模型自主委派的路径（`declared=false`）。要么把它
      固化成流程，要么说明边界太宽、模型在替人做路由。

    在编排出现之前这里只画实际，理由是「没有配置可对账，硬画等于编造」。
    现在有了配置，不画反而是丢掉了唯一能校验配置的信息。
    """
    scope = _scope_of(identity)
    start, end = _window(hours=hours, from_ts=None, to_ts=None)
    now = datetime.now(UTC)
    window = ObsWindow(start=start, end=end)

    if not runtime_settings.obs_enabled():
        return ObsTopologyResponse(
            hours=hours,
            enabled=False,
            generated_at=now,
            scope=scope,
            window=window,
        )

    if scope != "tenant":
        # 空集而不是 403：界面上要显示「需要 observability:read」，
        # 而不是一个看起来像坏掉的红框。
        return ObsTopologyResponse(
            hours=hours,
            enabled=True,
            generated_at=now,
            scope=scope,
            restricted=True,
            reason="委派拓扑需要 observability:read —— 边会暴露「别人在用哪个智能体」。",
            window=window,
        )

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            # 一次转交尝试一行。`metadata_json` 里带了 from / to / status /
            # reject_reason / depth，全部是写入时的快照。
            audit_rows = (
                await session.execute(
                    select(AuditLog.metadata_json, AuditLog.created_at).where(
                        AuditLog.tenant_id == identity.tenant_id,
                        AuditLog.event_type == AuditEventType.AGENT_DELEGATED,
                        AuditLog.created_at >= start,
                        AuditLog.created_at < end,
                    )
                )
            ).all()

            # 定义侧：谁现在还在、叫什么、能不能写。只用于显示，不参与计数——
            # 删掉一个 Agent，它历史被叫过的次数不该跟着消失。
            agent_rows = (
                await session.execute(
                    select(Agent.name, Agent.display_name, Agent.status, Agent.can_write).where(
                        Agent.tenant_id == identity.tenant_id
                    )
                )
            ).all()

            trace_rows = (
                await session.execute(
                    select(ObsTrace.agent_name, func.count(ObsTrace.id))
                    .where(
                        ObsTrace.tenant_id == identity.tenant_id,
                        ObsTrace.started_at >= start,
                        ObsTrace.started_at < end,
                    )
                    .group_by(ObsTrace.agent_name)
                )
            ).all()

            # 只取委派 span 的三列：`select(ObsSpan)` 会把 `content`
            # （full 模式下可达几十 KB）一起捞回来，这里一列都用不上。
            # `attrs.delegated_from` 要 JSON 解一下，但筛选条件已经把它压到
            # 「窗口内的委派次数」，不是全表扫 JSON（§4.1 提醒的是后者）。
            workflow_rows = (
                await session.execute(
                    select(Workflow.name, Workflow.definition).where(
                        Workflow.tenant_id == identity.tenant_id
                    )
                )
            ).all()

            span_rows = (
                await session.execute(
                    select(ObsSpan.agent_name, ObsSpan.attrs, ObsSpan.duration_ms).where(
                        ObsSpan.tenant_id == identity.tenant_id,
                        ObsSpan.kind == "agent",
                        ObsSpan.started_at >= start,
                        ObsSpan.started_at < end,
                    )
                )
            ).all()

    # ---- 聚合边 ----
    edges: dict[tuple[str | None, str, str], dict[str, Any]] = {}
    reasons: dict[tuple[str | None, str, str], Counter[str]] = {}
    depth_of: dict[str, int] = {}
    attempts: Counter[str] = Counter()
    observed: set[str] = set()

    for payload, created_at in audit_rows:
        meta = payload if isinstance(payload, dict) else {}

        to_agent = meta.get("to_agent")
        if not to_agent:
            # 老记录或写坏的行：宁可少一条边，也不猜一个接收方。
            continue

        from_agent = meta.get("from_agent") or None
        kind = EDGE_KIND_BY_STATUS.get(str(meta.get("status") or ""), "failed")
        key = (from_agent, str(to_agent), kind)

        item = edges.setdefault(key, {"count": 0, "last_at": None})
        item["count"] += 1
        ts = as_utc(created_at) if created_at else None
        if ts is not None and (item["last_at"] is None or ts > item["last_at"]):
            item["last_at"] = ts

        # 数「有人试图交给它」：被拒的也算。与 `/agents` 里按 span 数的
        # `delegated_turns` 不是同一个数，所以这里换个名字（`attempts`），
        # 免得两张表上的同名数字被人拿去对账。
        attempts[str(to_agent)] += 1

        reason = meta.get("reject_reason")
        if reason:
            reasons.setdefault(key, Counter())[str(reason)[:48]] += 1

        observed.add(str(to_agent))
        if from_agent:
            observed.add(from_agent)

        hop = meta.get("depth")
        if isinstance(hop, int) and hop > 0:
            previous = depth_of.get(str(to_agent))
            depth_of[str(to_agent)] = hop if previous is None else min(previous, hop)

    # ---- 边的耗时来自 span ----
    # 键是 (发起方, 接收方)，不含 kind：只有 `ok` 边用得上，
    # 而被拒/暂停的耗时根本不存在。
    latencies: dict[tuple[str | None, str], list[int]] = {}
    for span_agent, attrs, duration_ms in span_rows:
        payload = attrs if isinstance(attrs, dict) else {}
        # 存量行的 `agent_name` 是空的（V1 加这一列之前写的），但 attrs 里
        # 一直记着 `agent`——不回退到它，那几条边的耗时永远是空的。
        target = span_agent or payload.get("agent")
        if not target or duration_ms is None:
            continue
        parent = payload.get("delegated_from") or None
        latencies.setdefault((parent, str(target)), []).append(int(duration_ms))

    edge_items: list[ObsTopologyEdge] = []
    for (from_agent, to_agent, kind), item in edges.items():
        samples = sorted(latencies.get((from_agent, to_agent), []))
        counter = reasons.get((from_agent, to_agent, kind))
        edge_items.append(
            ObsTopologyEdge(
                from_agent=from_agent,
                to_agent=to_agent,
                kind=kind,  # type: ignore[arg-type]
                count=int(item["count"]),
                last_at=item["last_at"],
                p95_ms=_pct(samples, 0.95) if kind == "ok" else None,
                reasons=[
                    ObsBucket(key=str(k), count=int(c))
                    for k, c in (counter.most_common(TOPOLOGY_MAX_REASONS) if counter else [])
                ],
            )
        )

    # 稳定排序：同一张图刷新两次不能自己换位置。
    edge_items.sort(key=lambda e: (e.from_agent or "", e.to_agent, e.kind))

    # ---- 声明层 ----
    # 坏图跳过而不是整张接口报错：一张存量的坏配置不该让拓扑页打不开。
    declared_map: dict[tuple[str | None, str], set[str]] = {}
    invalid_workflows = 0

    for name, definition in workflow_rows:
        try:
            plan = compile_definition(definition)
        except DefinitionError:
            invalid_workflows += 1
            continue
        for source, target in agent_edges(plan):
            declared_map.setdefault((source or None, str(target)), set()).add(str(name))

    if invalid_workflows:
        logger.warning(
            "有编排的图不成立，已跳过其声明边。",
            extra={"event": "topology_bad_workflow", "count": invalid_workflows},
        )

    observed_pairs = {(edge.from_agent, edge.to_agent) for edge in edge_items}

    declared_items = [
        ObsDeclaredEdge(
            from_agent=source,
            to_agent=target,
            workflows=sorted(names),
            observed=(source, target) in observed_pairs,
        )
        for (source, target), names in sorted(
            declared_map.items(), key=lambda item: ((item[0][0] or ""), item[0][1])
        )
    ]

    for edge in edge_items:
        edge.declared = (edge.from_agent, edge.to_agent) in declared_map

    # ---- 节点 ----
    configured: dict[str, tuple[str | None, Any, bool]] = {
        str(name): (display, status_value, bool(can_write))
        for name, display, status_value, can_write in agent_rows
    }
    trace_counts = {str(name): int(count) for name, count in trace_rows if name}

    nodes: list[ObsTopologyNode] = []
    seen: set[str | None] = set()

    # 平台基线也在委派：没有名字的那一端单独一个节点，不能凭空消失，
    # 否则边上会出现一条指向不存在节点的线。
    if any(from_agent is None for (from_agent, _, _) in edges):
        nodes.append(
            ObsTopologyNode(
                name=None,
                configured=False,
                observed=True,
                depth=0,
                traces=0,
                attempts=0,
            )
        )
        seen.add(None)

    for name in sorted(configured) + sorted(observed | set(trace_counts)):
        if name in seen:
            continue
        seen.add(name)
        display, status_value, can_write = configured.get(name, (None, None, False))
        touched = name in observed or name in trace_counts
        nodes.append(
            ObsTopologyNode(
                name=name,
                display_name=display,
                status=getattr(status_value, "value", status_value),
                configured=name in configured,
                observed=touched,
                can_write=can_write,
                # 发起方（没有任何入边）算第 0 层。配置了但从没活动过的
                # 留在 None：它不在图上，也没有「第几跳」可言。
                depth=depth_of.get(name, 0 if touched else None),
                traces=trace_counts.get(name, 0),
                attempts=attempts.get(name, 0),
            )
        )

    nodes.sort(
        key=lambda n: (
            n.depth is None,
            n.depth if n.depth is not None else 0,
            -n.attempts,
            -n.traces,
            n.name or "",
        )
    )

    return ObsTopologyResponse(
        hours=hours,
        enabled=True,
        generated_at=now,
        scope=scope,
        window=window,
        nodes=nodes,
        edges=edge_items,
        declared_edges=declared_items,
        workflow_count=len(workflow_rows),
    )


def _elapsed_ms(since: datetime, now: datetime) -> int:
    """两个时间点之间多少毫秒。负数按 0 算——时钟回拨时不该显示「-3 秒」。"""
    return max(0, int((now - as_utc(since)).total_seconds() * 1000))


LIVE_LIMIT = 50


@router.get("/live", response_model=ObsLiveResponse)
async def live_now(
    limit: int = Query(default=LIVE_LIMIT, ge=1, le=200),
    identity: Identity = Depends(get_current_identity),
) -> ObsLiveResponse:
    """现在谁在跑、谁在等人工。

    只扫两类行：未收尾的 trace、`PENDING` 的审批单。数量天然少，所以不需要
    时间窗参数，也不用分页——这跟 `/traces` 是两种查询。

    两个刻意的选择：

    1. **`OBS_ENABLED=false` 时回 `enabled=false` + 空列表，不是 404。**
       关掉观测不等于接口不存在。界面要区分「现在看不到」和「这个功能坏了」，
       404 会把前者说成后者。
    2. **不返回「跑到了第几步」。** span 仍然整棵树攒到收尾才提交，
       运行中的 trace 一条 span 都没有。硬返回一个 0 会让人以为它还没开始干活，
       不如明确告诉调用方：这一层只能回答「跑了多久、谁在跑」。

    `self` 身份下两组都按 `user_id` 收——别人在等什么单子，跟他没关系。
    """
    scope = _scope_of(identity)
    stale_after_ms = settings.obs_live_stale_seconds * 1000
    now = datetime.now(UTC)

    if not runtime_settings.obs_enabled():
        return ObsLiveResponse(
            enabled=False,
            generated_at=now,
            stale_after_ms=stale_after_ms,
            scope=scope,
            running=[],
            waiting=[],
            truncated=False,
        )

    run_conditions = [
        ObsTrace.tenant_id == identity.tenant_id,
        ObsTrace.status == "running",
    ]
    wait_conditions = [
        ApprovalRequest.tenant_id == identity.tenant_id,
        ApprovalRequest.status == ApprovalStatus.PENDING,
    ]
    if scope == "self":
        run_conditions.append(ObsTrace.user_id == identity.user_id)
        wait_conditions.append(ApprovalRequest.user_id == identity.user_id)

    # 多取一条判截断，不另做 count：这个接口 5 秒被轮一次，
    # 多一次全表扫描只为显示一个总数不划算。
    fetch = limit + 1

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            run_rows = (
                await session.scalars(
                    select(ObsTrace)
                    .where(*run_conditions)
                    # 开始得最早的排最前：它最可能已经卡住了。
                    .order_by(ObsTrace.started_at.asc())
                    .limit(fetch)
                )
            ).all()
            wait_rows = (
                await session.scalars(
                    select(ApprovalRequest)
                    .where(*wait_conditions)
                    .order_by(ApprovalRequest.created_at.asc())
                    .limit(fetch)
                )
            ).all()

    running = []
    for row in run_rows[:limit]:
        age_ms = _elapsed_ms(row.started_at, now)
        running.append(
            ObsLiveRun(
                trace_id=row.trace_id,
                operation_id=row.operation_id or row.trace_id,
                conversation_id=row.conversation_id,
                user_id=row.user_id,
                agent_name=row.agent_name,
                started_at=row.started_at,
                age_ms=age_ms,
                stale=age_ms > stale_after_ms,
            )
        )

    waiting = []
    for row in wait_rows[:limit]:
        # 委派链的最后一项是直接上级（执行这张单子的 Agent 不在链上）。
        chain = row.delegation_chain or []
        parent = chain[-1] if chain and isinstance(chain[-1], dict) else {}
        waited_ms = _elapsed_ms(row.created_at, now)
        waiting.append(
            ObsLiveWaiting(
                approval_id=str(row.id),
                thread_id=row.thread_id,
                operation_id=row.operation_id,
                tool_name=row.tool_name,
                tool_display_name=row.tool_display_name,
                agent_name=row.agent_name,
                delegated_from=parent.get("name"),
                created_at=row.created_at,
                waited_ms=waited_ms,
                expires_at=row.expires_at,
                stale=waited_ms > stale_after_ms,
            )
        )

    return ObsLiveResponse(
        enabled=True,
        generated_at=now,
        stale_after_ms=stale_after_ms,
        scope=scope,
        running=running,
        waiting=waiting,
        truncated=len(run_rows) > limit or len(wait_rows) > limit,
    )


@router.get("/traces", response_model=ObsTraceListResponse)
async def list_traces(
    hours: int = Query(default=168, ge=1, le=MAX_WINDOW_HOURS),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    status: str | None = Query(default=None, max_length=24),
    intent: str | None = Query(default=None, max_length=64),
    tool_name: str | None = Query(default=None, max_length=64),
    skill: str | None = Query(default=None, max_length=64),
    agent: str | None = Query(default=None, max_length=64),
    conversation_id: uuid.UUID | None = Query(default=None),
    user_id: str | None = Query(default=None, max_length=128),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(get_current_identity),
) -> ObsTraceListResponse:
    """按时间倒序列出 trace。

    `q` 会扫 `input_text` / `output_text`，没有索引，只在时间窗内匹配——
    这是「想不起是哪一轮、但记得那句话」时的补救手段，不适合当主查询用。
    """
    scope = _scope_of(identity)
    start, end = _window(hours=hours, from_ts=from_ts, to_ts=to_ts)

    conditions = [
        ObsTrace.tenant_id == identity.tenant_id,
        ObsTrace.started_at >= start,
        ObsTrace.started_at < end,
    ]
    if scope == "self":
        conditions.append(ObsTrace.user_id == identity.user_id)
    elif user_id:
        # 只有能看整个租户的身份才允许把范围收窄到某个人；
        # 「自己」身份传 user_id 也不生效（上层已经把条件钉死了）。
        conditions.append(ObsTrace.user_id == user_id)

    if status:
        conditions.append(ObsTrace.status == status)
    if intent:
        conditions.append(ObsTrace.intent == intent)
    if tool_name:
        conditions.append(ObsTrace.tool_name == tool_name)
    if skill:
        # 筛的是「这一轮用没用到该技能」，不是「用到了哪个工具」。
        # 走 skill_name 列而不是扫 attrs：列表页是高频查询。
        conditions.append(ObsTrace.skill_name == skill)
    if agent:
        # 按「谁在做事」筛。存量行没有 agent_name（那时还没这个概念），
        # 所以筛具体名字时不会把它们算进来——要看历史全貌就别带这个条件。
        conditions.append(ObsTrace.agent_name == agent)
    if conversation_id:
        conditions.append(ObsTrace.conversation_id == conversation_id)
    if q:
        pattern = f"%{q}%"
        conditions.append(
            or_(
                ObsTrace.input_text.ilike(pattern),
                ObsTrace.output_text.ilike(pattern),
            )
        )

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            total = await session.scalar(select(func.count(ObsTrace.id)).where(*conditions))
            rows = (
                await session.scalars(
                    select(ObsTrace)
                    .where(*conditions)
                    .order_by(ObsTrace.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()

    return ObsTraceListResponse(
        items=[_summary(row) for row in rows],
        total=int(total or 0),
        scope=scope,
    )


async def _load_trace(
    session: AsyncSession,
    *,
    identity: Identity,
    scope: str,
    trace_id: str,
) -> ObsTrace:
    stmt = select(ObsTrace).where(ObsTrace.trace_id == trace_id)
    stmt = _scoped(stmt, identity=identity, scope=scope)
    row = await session.scalar(stmt)
    if row is None:
        # 别人的 trace 一律 404：403 等于告诉对方「这个 id 存在」。
        raise HTTPException(status_code=404, detail="trace 不存在或已过保留期。")
    return row


@router.get("/traces/{trace_id}", response_model=ObsTraceDetailResponse)
async def get_trace(
    trace_id: str,
    include_content: bool = Query(default=True),
    identity: Identity = Depends(get_current_identity),
) -> ObsTraceDetailResponse:
    """单条 trace 的完整明细：技能注入情况、各阶段 span 与原文。

    `include_content=false` 用于只看耗时结构：省掉每个 span 的原文，
    一轮十几万字符的对话在窄带宽下差别很明显。
    """
    scope = _scope_of(identity)

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await _load_trace(session, identity=identity, scope=scope, trace_id=trace_id)
            spans = (
                await session.scalars(
                    select(ObsSpan)
                    .where(
                        ObsSpan.tenant_id == identity.tenant_id,
                        ObsSpan.trace_id == trace_id,
                    )
                    .order_by(ObsSpan.seq.asc(), ObsSpan.started_at.asc())
                )
            ).all()
            insight = await _skill_insight(session, trace=row, spans=list(spans))

    return ObsTraceDetailResponse(
        trace=_summary(row),
        input_text=row.input_text,
        output_text=row.output_text,
        attrs=row.attrs or {},
        spans=[
            ObsSpanItem(
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                seq=item.seq or 0,
                name=item.name,
                kind=item.kind,
                agent_name=item.agent_name,
                started_at=item.started_at,
                duration_ms=item.duration_ms,
                status=item.status or "ok",
                error_category=item.error_category,
                content=(item.content if include_content else None),
                attrs=item.attrs or {},
            )
            for item in spans
        ],
        skill=insight,
    )


# ---------------------------------------------------------------- 单次运行（L3）

# 一次运行由多条 trace、若干张审批单组成，每一段各有自己的状态。
# 聚合时取「最需要人看一眼」的那一个，顺序与 `display_state` 内部一致：
# 否决 → 失败 → 等人工 → 在跑 → 取消 → 完成。
#
# 「否决优先于失败」在这里同样是必须的：`DELEGATION_REVOKED` 会让某一轮落成
# error，但那时执行方可能完全健康，被收窄的是上游。显示 `failed` 会把运维
# 引去查一个没问题的 Agent——这条规则在单条 trace 上写过一次（`state.py`），
# 跨轮聚合不能因为「换了个函数」就换一套顺序。
OPERATION_STATE_ORDER = ("rejected", "failed", "input-required", "working", "canceled", "completed")


def _worst_state(states: list[str]) -> str:
    rank = {name: index for index, name in enumerate(OPERATION_STATE_ORDER)}
    worst = "completed"
    for state in states:
        if rank.get(state, len(rank)) < rank.get(worst, len(rank)):
            worst = state
    return worst


def _trace_state(row: ObsTrace, *, superseded: bool) -> str:
    """某一轮在这次运行里的状态。`superseded` = 后面还有一轮。

    「停在审批上」是**那一轮**的结论，不是这次运行的结论。它后面如果还有
    一轮（确认之后执行），那个暂停早就被处理掉了——不抹掉的话，一次上午
    就跑完的写操作会永远显示「等人工」，而这是个会让人点进去看一眼的状态。
    反过来，最后一轮仍是 `awaiting_approval`（单子还挂着，或者审批单的
    `operation_id` 缺失导致查不到），就老老实实算「等人工」，不猜它已经处理完了。
    """
    state = display_state(
        trace_status=row.status,
        approval_status="PENDING" if row.status == "awaiting_approval" else None,
        error_category=row.error_category,
    )
    if state == "input-required" and superseded:
        return "completed"
    return state


def _merge_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    """区间并集的总长度（毫秒）。

    **不能求和。** 同一段时间里可能同时挂着两张单子（一次运行里两个写操作
    各要一次确认），求和会把同一分钟算两遍，算出来的「等人工」甚至比总历时
    还长——那种数一眼就是错的，但错法很隐蔽：只有并行审批时才会出现。

    并集才是「这次运行里有多少时间是卡在人这儿」的本义。
    """
    points = sorted(
        (as_utc(start), as_utc(end)) for start, end in intervals if as_utc(end) > as_utc(start)
    )
    if not points:
        return 0

    total = 0.0
    current_start, current_end = points[0]
    for start, end in points[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += (current_end - current_start).total_seconds()
        current_start, current_end = start, end
    total += (current_end - current_start).total_seconds()
    return max(0, int(total * 1000))


def _approval_wait(row: ApprovalRequest, now: datetime) -> tuple[datetime, datetime]:
    """一张单子「在等人」的那一段：从生成到有人处理。

    终点按这个顺序取：
    1. `approved_at`——确认那一刻等待就结束了，之后的执行是系统在跑；
    2. `completed_at`——被驳回或执行收尾（驳回没有 `approved_at`）；
    3. `expires_at`——过期作废，那一天就是等待的尽头；
    4. 都为空 = 现在还挂着，算到此刻。

    不做「有没有人真的看过」的判断：平台没有「已读」这个概念，
    挂着的每一秒都是这次运行在等人。
    """
    created = as_utc(row.created_at)
    if row.approved_at is not None:
        end = as_utc(row.approved_at)
    elif row.completed_at is not None:
        end = as_utc(row.completed_at)
    elif _enum_value(row.status) == ApprovalStatus.EXPIRED.value:
        end = as_utc(row.expires_at)
    else:
        end = now
    return created, max(created, end)


def _enum_value(value: object) -> str:
    """Enum → 它的值。

    `class ApprovalStatus(str, Enum)` 在 3.11 里 `str(x)` 得到的是
    `"ApprovalStatus.PENDING"`，拿去和 `"PENDING"` 比永远不相等。
    """
    return str(getattr(value, "value", value) or "")


@router.get("/operations/{operation_id}", response_model=ObsOperationResponse)
async def get_operation(
    operation_id: str,
    identity: Identity = Depends(get_current_identity),
) -> ObsOperationResponse:
    """一次运行：跨轮、跨 Agent 的一条时间线。

    ## 三个耗时必须分开

    - `wall_ms`：第一轮开始 → 最后一轮结束（还挂着单子就到此刻）；
    - `waiting_ms`：其中卡在等人点确认的时间，**是 `wall_ms` 的一部分**；
    - `active_ms`：两者之差，系统真正在跑的时间。

    这不是「多给一个指标」。一次运行历时 3 小时、其中 2 小时 58 分在等人，
    和「系统跑了 3 小时」是两个完全不同的结论——前者要改的是审批路径，
    后者要改的是模型或工具。合成一个数，这条运行最值钱的信息就没了。

    ## 分组键不是权限边界

    `operation_id` 只用来把这几个 trace 串起来。`self` 身份查别人的运行
    **回 404 而不是 403**：403 等于告诉对方「这个 id 是存在的」。

    ## 观测关掉时

    回 `enabled=false` + 空列表。审批单本身还在库里（它是业务事实），
    但没有 trace 就画不出时间线，只有单子的运行页会误导人。
    """
    scope = _scope_of(identity)
    now = datetime.now(UTC)

    if not runtime_settings.obs_enabled():
        return ObsOperationResponse(
            operation_id=operation_id,
            enabled=False,
            generated_at=now,
            scope=scope,
        )

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            # `OR trace_id = :id` 兜的是 V1 之前的存量行：那时没有 operation_id，
            # 界面按 `operation_id or trace_id` 显示，于是点进去会查这个 id。
            # 不加这一条，那些行点开就是 404。
            trace_stmt = select(ObsTrace).where(
                or_(
                    ObsTrace.operation_id == operation_id,
                    ObsTrace.trace_id == operation_id,
                )
            )
            trace_rows = (
                await session.scalars(
                    _scoped(trace_stmt, identity=identity, scope=scope).order_by(
                        ObsTrace.started_at.asc()
                    )
                )
            ).all()

            approval_conditions = [
                ApprovalRequest.tenant_id == identity.tenant_id,
                ApprovalRequest.operation_id == operation_id,
            ]
            if scope == "self":
                approval_conditions.append(ApprovalRequest.user_id == identity.user_id)
            approval_rows = (
                await session.scalars(
                    select(ApprovalRequest)
                    .where(*approval_conditions)
                    .order_by(ApprovalRequest.created_at.asc())
                )
            ).all()

            if not trace_rows and not approval_rows:
                raise HTTPException(status_code=404, detail="运行不存在或已过保留期。")

            trace_ids = [row.trace_id for row in trace_rows]
            span_rows = (
                (
                    await session.execute(
                        # 只取六列：`select(ObsSpan)` 会把 `content`
                        # （full 模式下几十 KB）一起捞回来，这里一列都用不上。
                        select(
                            ObsSpan.trace_id,
                            ObsSpan.agent_name,
                            ObsSpan.attrs,
                            ObsSpan.duration_ms,
                            ObsSpan.status,
                            ObsSpan.started_at,
                        ).where(
                            ObsSpan.tenant_id == identity.tenant_id,
                            ObsSpan.kind == "agent",
                            ObsSpan.trace_id.in_(trace_ids),
                        )
                    )
                ).all()
                if trace_ids
                else []
            )

            names = {
                *(row.agent_name for row in trace_rows if row.agent_name),
                *(row.agent_name for row in approval_rows if row.agent_name),
                *(span.agent_name for span in span_rows if span.agent_name),
            }
            agent_rows = (
                (
                    await session.execute(
                        select(Agent.name, Agent.display_name).where(
                            Agent.tenant_id == identity.tenant_id,
                            Agent.name.in_(names),
                        )
                    )
                ).all()
                if names
                else []
            )

    display_names = {name: display for name, display in agent_rows}

    # ---- 每一轮 + 这一轮里的转交 ----
    delegations_of: dict[str, list[ObsOperationDelegation]] = {}
    for span in span_rows:
        attrs = span.attrs if isinstance(span.attrs, dict) else {}
        target = span.agent_name or attrs.get("agent")
        if not target:
            continue
        depth = attrs.get("depth")
        delegations_of.setdefault(span.trace_id, []).append(
            ObsOperationDelegation(
                agent=str(target),
                delegated_from=attrs.get("delegated_from") or None,
                depth=int(depth) if isinstance(depth, int) else None,
                duration_ms=span.duration_ms,
                status=span.status or "ok",
                tool_calls=_int_or_none(attrs.get("tool_calls")) or 0,
            )
        )

    last_start = max((as_utc(row.started_at) for row in trace_rows), default=None)
    traces = [
        ObsOperationTrace(
            **{
                **_summary(row).model_dump(),
                "display_state": _trace_state(
                    row,
                    superseded=last_start is not None and as_utc(row.started_at) < last_start,
                ),
            },
            delegations=delegations_of.get(row.trace_id, []),
        )
        for row in trace_rows
    ]

    # ---- 审批单 ----
    approvals: list[ObsOperationApproval] = []
    wait_intervals: list[tuple[datetime, datetime]] = []
    wait_by_agent: dict[str, list[tuple[datetime, datetime]]] = {}
    pending = False
    for row in approval_rows:
        status_value = _enum_value(row.status)
        pending = pending or status_value == ApprovalStatus.PENDING.value
        chain = [
            str(item.get("name") or "")
            for item in (row.delegation_chain or [])
            if isinstance(item, dict)
        ]
        start, end = _approval_wait(row, now)
        wait_intervals.append((start, end))
        if row.agent_name:
            wait_by_agent.setdefault(row.agent_name, []).append((start, end))

        approvals.append(
            ObsOperationApproval(
                approval_id=str(row.id),
                tool_name=row.tool_name,
                tool_display_name=row.tool_display_name,
                risk_level=_enum_value(row.risk_level) or None,
                status=status_value,
                agent_name=row.agent_name,
                delegated_from=row.delegated_from,
                delegation_chain=chain,
                reason=row.reason or "",
                created_at=row.created_at,
                approved_at=row.approved_at,
                approved_by=row.approved_by,
                completed_at=row.completed_at,
                waited_ms=_elapsed_ms(start, end),
                error_code=row.error_code,
                display_state=display_state(
                    trace_status=None,
                    approval_status=status_value,
                ),
            )
        )

    waiting_ms = _merge_ms(wait_intervals)

    # ---- 起止与三个耗时 ----
    starts = [as_utc(row.started_at) for row in trace_rows]
    starts += [start for start, _ in wait_intervals]
    started_at = min(starts) if starts else None

    ends = [as_utc(row.ended_at) if row.ended_at else now for row in trace_rows]
    ends += [end for _, end in wait_intervals]
    ended_at = max(ends) if ends else started_at

    wall_ms = _elapsed_ms(started_at, ended_at) if started_at and ended_at else 0
    # 等待区间可能有一小段落在 wall 之外（时钟回拨、或首轮 trace 在单子生成
    # 之前就记了结束），扣的时候夹住下界：宁可是 0，也不给一个负数。
    active_ms = max(0, wall_ms - waiting_ms)

    # ---- 参与者 ----
    # 「谁发起的、谁被叫来干活、谁又往外派了」——审批人事后回溯要靠这一栏。
    # 用 agent_name 做键，`None`（平台基线）单独一项，不并到任何 Agent 身上。
    participants: dict[str | None, ObsOperationParticipant] = {}

    def participant(name: str | None) -> ObsOperationParticipant:
        item = participants.get(name)
        if item is None:
            item = ObsOperationParticipant(
                agent_name=name,
                display_name=display_names.get(name) if name else None,
            )
            participants[name] = item
        return item

    for row in trace_rows:
        item = participant(row.agent_name)
        item.traces += 1
        at = as_utc(row.started_at)
        item.first_at = min(item.first_at, at) if item.first_at else at
        last = as_utc(row.ended_at) if row.ended_at else at
        item.last_at = max(item.last_at, last) if item.last_at else last

    for span in span_rows:
        attrs = span.attrs if isinstance(span.attrs, dict) else {}
        target = span.agent_name or attrs.get("agent")
        if target:
            participant(str(target)).delegated_in += 1
        parent = attrs.get("delegated_from")
        if parent:
            participant(str(parent)).delegated_out += 1

    for item in participants.values():
        item.waiting_ms = _merge_ms(wait_by_agent.get(item.agent_name or "", []))
        if item.traces and item.delegated_in:
            item.role = "both"
        elif item.delegated_in:
            item.role = "delegate"
        else:
            item.role = "initiator"

    ordered = sorted(
        participants.values(),
        key=lambda item: (
            item.first_at or ended_at or now,
            item.agent_name or "",
        ),
    )

    # ---- 时间线 ----
    steps: list[ObsOperationStep] = [
        ObsOperationStep(
            kind="trace",
            ref=item.trace_id,
            at=item.started_at,
            state=item.display_state,
            agent_name=item.agent_name,
            duration_ms=item.duration_ms,
        )
        for item in traces
    ]
    steps += [
        ObsOperationStep(
            kind="approval",
            ref=item.approval_id,
            at=item.created_at,
            state=item.display_state,
            agent_name=item.agent_name,
            tool_display_name=item.tool_display_name,
        )
        for item in approvals
    ]
    # 同一时刻先放 trace 再放单子：单子是在那一轮里开出来的。
    steps.sort(key=lambda step: (step.at, 0 if step.kind == "trace" else 1))

    state = _worst_state([item.display_state for item in traces + approvals])
    running = any(row.status == "running" for row in trace_rows)
    attention_reason = "pending_approval" if pending else ("running" if running else None)

    return ObsOperationResponse(
        operation_id=operation_id,
        enabled=True,
        generated_at=now,
        scope=scope,
        user_id=(trace_rows[0].user_id if trace_rows else approval_rows[0].user_id),
        started_at=started_at,
        ended_at=ended_at,
        wall_ms=wall_ms,
        waiting_ms=waiting_ms,
        active_ms=active_ms,
        display_state=state,
        attention=needs_attention(
            display_state_value=state,
            approval_status="PENDING" if pending else None,
            trace_status="running" if running else None,
        ),
        attention_reason=attention_reason,
        traces=traces,
        approvals=approvals,
        participants=ordered,
        steps=steps,
    )


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _skill_attrs(spans: list[ObsSpan]) -> dict:
    """取本轮 `llm.call` 上的技能属性。

    一轮里有多次模型调用（工具执行完再规划），技能信息每次都一样，
    取第一条有值的就够了——按 seq 而不是按时间，seq 是写入顺序。
    """
    for item in sorted(spans, key=lambda s: s.seq or 0):
        attrs = item.attrs or {}
        if attrs.get("skill.name"):
            return attrs
    return {}


async def _skill_insight(
    session: AsyncSession,
    *,
    trace: ObsTrace,
    spans: list[ObsSpan],
) -> ObsSkillInsight | None:
    """本轮技能命中情况：当时注入了什么 + 近 7 天被用过几次。

    三个来源刻意分开取，别互相覆盖：

    - 注入信息取自 span attrs（当时的事实，技能后来改正文也不影响这一条）；
    - 命中统计取自 `skill_hits`（不看观测开关，也不受保留期影响）；
    - `exists` / `enabled` 取自 `skills` 当前状态（技能可能已删或已停用）。
    """
    name = trace.skill_name
    if not name:
        return None

    attrs = _skill_attrs(spans)
    insight = ObsSkillInsight(
        name=name,
        matched_by=attrs.get("skill.matched_by"),
        instruction_chars=_int_or_none(attrs.get("skill.chars")),
        instruction_limit=_int_or_none(attrs.get("skill.limit")),
        truncated=bool(attrs.get("skill.truncated")),
        tools_before=_int_or_none(attrs.get("skill.tools_before")),
        tools_after=_int_or_none(attrs.get("skill.tools_after")),
    )

    # 这一轮是否真的记了命中。用 trace_id 反查，比“信 attrs”可靠：
    # 记录失败时 nodes 只 warning，页面不该显示成已记录。
    hit = await session.scalar(select(SkillHit).where(SkillHit.trace_id == trace.trace_id).limit(1))
    if hit is not None:
        insight.recorded = True
        insight.matched_by = hit.matched_by or insight.matched_by
        insight.keyword = hit.keyword

    skill = await session.scalar(select(Skill).where(Skill.name == name))
    if skill is None:
        # 删技能会级联删掉命中记录，所以这里查不到行时，统计也只能是 0。
        insight.exists = False
        return insight

    insight.enabled = bool(skill.enabled)
    since = datetime.now(UTC) - timedelta(days=skill_service.HITS_WINDOW_DAYS)
    insight.hits_7d = int(
        await session.scalar(
            select(func.count(SkillHit.id)).where(
                SkillHit.skill_id == skill.id,
                SkillHit.created_at >= since,
            )
        )
        or 0
    )
    insight.last_hit_at = await session.scalar(
        select(func.max(SkillHit.created_at)).where(SkillHit.skill_id == skill.id)
    )
    return insight
