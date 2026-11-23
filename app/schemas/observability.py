# app/schemas/observability.py
"""观测查询接口的请求/响应模型。

两条约束贯穿整个文件：

1. **列表不带原文**。`input_text` / `output_text` / `content` 是整轮 prompt 与工具
   返回，单条可达几十 KB；列表页只展示汇总列，原文只在详情接口给。
2. **时间一律带时区**。库里存的是 `timestamptz`，出参统一转 UTC，
   由前端按本地时区渲染——否则跨时区看时间会整体偏移。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import APIModel, as_utc


class ObsTraceSummary(APIModel):
    """一轮对话的汇总行，对应列表页一行。"""

    trace_id: str
    # 一次运行的分组键（= 发起那一轮的 trace_id）。审批确认后执行的 trace
    # 继承同一个值，于是「发起」与「执行」在运行视图里是同一次运行。
    operation_id: str | None = None
    conversation_id: uuid.UUID | None = None
    user_id: str
    # 这一轮由哪个智能体执行。存量行是 NULL（那时还没有 agent 这个概念），
    # 不回溯填充——按 null 展示成「默认助手」，比编一个名字更诚实。
    agent_name: str | None = None
    started_at: datetime
    duration_ms: int | None = None
    status: str
    error_category: str | None = None
    intent: str | None = None
    tool_name: str | None = None
    # 本轮命中的技能。未命中为 None，列表页据此显示技能标签。
    skill_name: str | None = None
    # 三套状态词表压出来的一个界面状态（working / input-required / completed /
    # failed / rejected / canceled）。与 `status` 并列而不是替换它：
    # 原始状态是排查依据，派生状态是给人看的那一个。
    display_state: str = "completed"
    span_count: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_micros: int | None = None

    @field_validator("started_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsTraceListResponse(APIModel):
    items: list[ObsTraceSummary] = Field(default_factory=list)
    total: int = 0
    # 当前身份能看到的范围，前端据此提示「只显示你自己的」还是「整个租户」。
    scope: str = "self"


class ObsSpanItem(APIModel):
    """一条 span。父子关系交给前端建树，服务端只保证 seq 有序。"""

    span_id: str
    parent_span_id: str | None = None
    seq: int = 0
    name: str
    kind: str
    # 这个 span 属于哪个智能体。委派时子图里的节点归被委派方。
    # 存量行为 None（那时还没有这个维度），界面按「未归属」展示。
    agent_name: str | None = None
    started_at: datetime
    duration_ms: int | None = None
    status: str = "ok"
    error_category: str | None = None
    content: str | None = None
    attrs: dict = Field(default_factory=dict)

    @field_validator("started_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsSkillInsight(APIModel):
    """详情页「Skill 选择」那一节。

    数据分三个来源，因为它们的可靠性不一样：

    - 这一次的注入情况（字符数、收窄前后工具数）来自 span attrs——它是
      **当时**的事实，技能后来改了正文也不影响这一条；
    - 命中次数与最近命中来自 `skill_hits`——它不看观测开关，也不受保留期影响；
    - `exists` / `enabled` 来自 `skills` 当前状态——技能可能已经删了或停了，
      详情要能说清「这一轮用的技能现在还在不在」。
    """

    name: str
    matched_by: str | None = None
    keyword: str | None = None
    hits_7d: int = 0
    last_hit_at: datetime | None = None
    # 这一轮在 skill_hits 里留下了记录。观测可能没开、技能可能刚被删，
    # 「有命中统计」与「这一轮记了」是两件事。
    recorded: bool = False
    instruction_chars: int | None = None
    instruction_limit: int | None = None
    truncated: bool = False
    tools_before: int | None = None
    tools_after: int | None = None
    exists: bool = True
    enabled: bool | None = None

    @field_validator("last_hit_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsTraceDetailResponse(APIModel):
    trace: ObsTraceSummary
    input_text: str | None = None
    output_text: str | None = None
    attrs: dict = Field(default_factory=dict)
    spans: list[ObsSpanItem] = Field(default_factory=list)
    # 本轮命中的技能及其命中统计；未命中为 None。
    skill: ObsSkillInsight | None = None


class ObsBucket(APIModel):
    """分布统计的一格，例如 status=error 有 3 条。"""

    key: str
    count: int = 0


class ObsToolStat(APIModel):
    tool_name: str
    calls: int = 0
    errors: int = 0
    avg_duration_ms: int | None = None


class ObsOverviewBucket(APIModel):
    """概览走势图上的一格，覆盖 `[t - bucket_hours, t)`。

    切法与 `ObsAgentBucket` 同一套（从窗口末尾往前切，最后一格收在「现在」，
    只有最旧一格可能被窗口起点截断），两个接口的图因此可以并排读。

    比 Agent 那一份多了两样：
    - `awaiting`：这一格里停在等人工的轮次。它既不算错误也不算正常，
      单独一色才不会在堆叠柱里被并进「其余」；
    - `p95_duration_ms`：**每一格自己的** p95。窗口级的那个分位数回答不了
      「是刚才变慢了还是一直这么慢」，而后者才是排障要问的。
    """

    t: datetime
    traces: int = 0
    errors: int = 0
    awaiting: int = 0
    p95_duration_ms: int | None = None
    # 被窗口起点截断的最旧一格。只有这一格可能为 True。
    partial: bool = False

    @field_validator("t")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsOverviewResponse(APIModel):
    """概览卡片的数据源。窗口由 `hours` 决定，未指定时按 24 小时。"""

    hours: int
    enabled: bool
    traces: int = 0
    errors: int = 0
    error_rate: float = 0.0
    # 停在等人确认的轮次。**不计入 `errors`**：那是设计好的暂停，不是失败
    # 单独成字段是为了让它们有地方可去——
    # 从错误率里摘掉之后如果什么都不留，界面上会像是「凭空少了二十轮」。
    #
    # 这是**窗口内曾经停过**的轮次，不是「此刻还挂着几张单子」。审批处理完之后
    # trace 仍保留 `awaiting_approval`——它记录的是那一轮当时的结论，回写历史
    # 等于改写历史（与「代理名字不回溯改写」同一条原则）。「现在还有谁在等」
    # 归 `/live` 回答，那边的数是查 PENDING 单子得来的。
    awaiting_approval: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    avg_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    max_duration_ms: int | None = None
    by_status: list[ObsBucket] = Field(default_factory=list)
    by_intent: list[ObsBucket] = Field(default_factory=list)
    # 窗口内命中过的技能，兼作「按技能筛选」的下拉选项。
    # 取的是 span/trace 的实际记录，已删除的历史技能也能出现——
    # 那正是排查「删了技能之后还有人用 /名字」时要看的东西。
    by_skill: list[ObsBucket] = Field(default_factory=list)
    # 按智能体分组的轮次。存量行（没有 agent_name）归到「—」这一格，
    # 这样各格之和等于 traces——分项加不平的总量会让人怀疑整张表。
    by_agent: list[ObsBucket] = Field(default_factory=list)
    top_tools: list[ObsToolStat] = Field(default_factory=list)
    # 时间维度：把窗口切成一格一格的走势。截面上那八个数字只能说明「多大」，
    # 回答不了「是刚变成这样的，还是一直这样」——而后者才决定要不要现在动手。
    buckets: list[ObsOverviewBucket] = Field(default_factory=list)
    # 一格的宽度（小时）。显式返回而不是让前端从相邻两个 `t` 相减：
    # 只有一格时减不出来，而「只有一格」恰恰是 1 小时窗的常态。
    bucket_hours: int = 0
    # 最近一条错误的时间，用来回答「昨天开始报错了吗」。
    last_error_at: datetime | None = None
    scope: str = "self"

    @field_validator("last_error_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


# 趋势里「算不出比例」的三种原因。写成封闭集合而不是裸 str：
# 这个值同时被控制台的提示文案与测试断言消费，多一个没人认识的取值
# 会静默显示成空，而空看起来又像「没有原因」。
#
# - no_baseline：上一窗口 0 轮，除不出比例；
# - created_after_baseline_start：Agent 在上一窗口开始之后才创建，那一段覆盖不全；
# - low_sample：任一侧不足 5 轮，比例只是噪声。
ObsChangeReason = Literal[
    "no_baseline",
    "created_after_baseline_start",
    "low_sample",
]


class ObsWindow(APIModel):
    """一段半开区间 `[start, end)`。返回给前端是为了让「比的是哪一段」可核对。"""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsAgentBucket(APIModel):
    """趋势图上的一格，覆盖 `[t - bucket_hours, t)`。

    `t` 是这一格的**结束**时刻，不是开始。分桶从窗口末尾往前切，所以除了可能被
    窗口起点截断的最旧一格（`partial=True`）以外，每格都是等长的完整格。刻意不按
    自然日对齐：滚动窗口一旦混进日历语义，首尾两格必然同时残缺，「周一早上看什么
    都像暴跌」。
    """

    t: datetime
    traces: int = 0
    errors: int = 0
    denials: int = 0
    # 被窗口起点截断的最旧一格。只有这一格可能为 True。
    partial: bool = False

    @field_validator("t")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsAgentStat(APIModel):
    """一个智能体在窗口内的运算基准。

    数值来自两处：`obs_traces`（轮次 / 错误 / 耗时 / token）与治理侧
    （被拒次数、审批单数）。两边记的 `agent_name` 都是**当时的快照**——
    Agent 改名或删除后，历史行仍归属当时的那个名字，不迁移、不回溯：
    把旧数据改写成新名字，会让「上个月这个 Agent 到底跑成什么样」失真。

    所以 `status` 为 `None` 表示「库里已经没有这个 Agent 了」，而不是出错。
    """

    agent_name: str
    display_name: str | None = None
    # active / draft / disabled；None = 已被删除（历史行还在）。
    status: str | None = None
    revision: int | None = None

    traces: int = 0
    errors: int = 0
    error_rate: float = 0.0
    # 这一行里停在等人确认的轮次，同样不计入 `errors`。见 `ObsOverviewResponse`。
    awaiting_approval: int = 0
    # 被别的智能体叫去干活的次数（`kind=agent` 的 span 数）。
    #
    # 与 `traces` 是两个数，**不能相加**：委派的子调用跑在发起方那条 trace 里，
    # 所以它不会在「被叫去」的次数里出现；反过来，一个从没被叫过的 Agent
    # 也可以有大量自己的轮次。要回答「谁被使唤、谁在使唤人」得分别看这两列。
    delegated_turns: int = 0
    # 它把活转交给了谁、各几次。来自 `agent_delegated` 审计，**含被拒的尝试**
    # （与 `delegated_turns` 数 span 的口径不同，见 `/topology` 的 `attempts`）。
    # 只给「谁」和「几次」：四种结局的拆分在拓扑图上，这里再分一遍就是
    # 第二处会对不上的数。最多 5 个，长尾去看拓扑。
    delegated_to: list[ObsBucket] = Field(default_factory=list)
    # 它名下的审批单在窗口内被等待的时长（区间并集，裁剪到窗口）。
    #
    # **这是「人」的时间，不是它的耗时。** 一个每轮都要等确认的 Agent
    # 如果把等待算进耗时，看起来就像是它自己慢——而真正慢的是流程。
    # 同一段窗口里多张单子并行挂着时取并集，不求和（求和会超过窗口长度）。
    waiting_ms: int = 0
    # 占窗口内全部轮次的比例，用来回答「是不是只有这一个在用」。
    share: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 模型没报单价时是 None，不是 0——「没算」和「花了 0」不是一回事。
    cost_micros: int | None = None
    avg_duration_ms: int | None = None
    p95_duration_ms: int | None = None

    # 治理侧：被边界/权限挡下的次数，与它发起的审批单。
    # 前者持续升高说明边界配窄了，后者持续被驳说明职责写宽了。
    denials: int = 0
    approvals: int = 0
    approvals_rejected: int = 0

    last_started_at: datetime | None = None

    # ---- 趋势：与上一个等长窗口相比 ----
    # 比的是**轮次**，不是 token 或成本：后两者有 null（没报单价），
    # 拿有缺口的量算比例会得出「涨了 300%」这种假结论。
    previous_traces: int = 0
    # `change` 为 None 时看 `change_reason`——「没法比」和「没变化」是两件事，
    # 前者绝不能被画成 0%。
    change: float | None = None
    change_reason: ObsChangeReason | None = None
    buckets: list[ObsAgentBucket] = Field(default_factory=list)

    @field_validator("last_started_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsLiveRun(APIModel):
    """正在跑的一轮（已落起始行、还没收尾）。

    **这里没有「当前 span 数」。** span 仍然是整棵树攒到收尾才提交
    （起始行提前落库只打破了这一半），所以运行中的 trace 没有任何
    span 明细可数——写一个恒为 0 的列比不写更容易被误读成「它还没开始干活」。
    要看它跑到哪一步，只有等这轮结束；运行视图能回答的是「跑了多久、谁在跑」。
    """

    trace_id: str
    # 一次运行的分组键。同一次运行里可能有「发起」与「确认后执行」两条 trace，
    # 它们会各占一行——因为它们是同时在跑的两件事，不是一条。
    operation_id: str | None = None
    conversation_id: uuid.UUID | None = None
    user_id: str
    agent_name: str | None = None
    started_at: datetime
    # 已经开始多久。超过 `stale_after_ms` 的标 stale：可能是模型在慢慢想，
    # 也可能是进程已经没了而清扫任务还没轮到——界面要让这两种看起来不一样。
    age_ms: int
    stale: bool = False

    @field_validator("started_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsLiveWaiting(APIModel):
    """一张挂着没人处理的审批单。"""

    approval_id: str
    thread_id: str
    operation_id: str | None = None
    tool_name: str
    tool_display_name: str | None = None
    agent_name: str | None = None
    # 委派链上的直接上级。为空表示这张单子是用户直接触发的，不是转交来的——
    # 界面上「谁该去点确认」的答案不同。
    delegated_from: str | None = None
    created_at: datetime
    waited_ms: int
    # 审批有效期。过期后维护任务会把它改成 EXPIRED，
    # 所以「还要多久作废」是这张视图能给出的最直接的行动信号。
    expires_at: datetime | None = None
    stale: bool = False

    @field_validator("created_at")
    @classmethod
    def _utc_created(cls, value: datetime) -> datetime:
        return as_utc(value)

    @field_validator("expires_at")
    @classmethod
    def _utc_expires(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsLiveResponse(APIModel):
    """「现在怎么了」：在跑 + 在等。

    两组都为空时前端整条横条不渲染——不留一个「0 个在跑」的空壳。
    """

    enabled: bool
    generated_at: datetime
    # 超过这个时长标 stale。放响应里而不是让前端写死：阈值是配置项。
    stale_after_ms: int
    scope: str = "self"
    running: list[ObsLiveRun] = Field(default_factory=list)
    waiting: list[ObsLiveWaiting] = Field(default_factory=list)
    # 被 limit 截断。界面要说明「只显示最近 N 条」，否则数出来的数对不上库。
    truncated: bool = False


class ObsAgentBaselineResponse(APIModel):
    """按智能体的运算基准。窗口规则与概览一致。"""

    hours: int
    enabled: bool
    total_traces: int = 0
    # 没有 agent_name 的存量行。单独列出来，不塞进某个 Agent，
    # 这样「agents 之和 + unattributed == total_traces」恒成立。
    unattributed_traces: int = 0
    agents: list[ObsAgentStat] = Field(default_factory=list)
    scope: str = "self"

    # 趋势用的两段区间与桶宽。`previous` 为 None 表示这次没算对比
    # （`compare=false`），而不是「上一窗口没有数据」——后者是
    # `previous_traces=0` 加 `change_reason="no_baseline"`。
    window: ObsWindow | None = None
    previous: ObsWindow | None = None
    bucket_hours: int = 0


# ---------------------------------------------------------------- 委派拓扑（L2）

# 一条委派边的四种结局。**不能合并成「成功 / 失败」两档**：
#
# - `rejected`：转交在边界校验处就被挡下，子图根本没进。被拒持续出现说明
#   上游边界配得太宽（A 以为自己能叫 B），或者下游配得更窄（B 现在做不了这事）——
#   两种都要改，但改的地方不同。把它画成「失败」会让人去查一个没问题的执行方。
# - `paused`：子调用停在审批上，等人点确认。不是故障，是设计好的暂停。
# - `failed`：转交成立但子调用崩了/超时/无证据。这才是「这条边不稳」。
ObsEdgeKind = Literal["ok", "paused", "rejected", "failed"]


class ObsTopologyNode(APIModel):
    """图上的一个节点。

    `name` 可以为 `None`：**平台基线（没配 Agent 的那个默认助手）也会委派**，
    那时 `agent_name` 就是空的。用 `None` 而不是塞一个占位字符串，是因为
    字符串挡不住「真有一个叫 `—` 的 Agent」——而这两件事在排障时的含义完全不同。
    存量 span 的 `agent_name` 为 NULL 也是同一种情况，一律按「未归属」画。
    """

    name: str | None = None
    display_name: str | None = None
    # active / draft / disabled；None = 库里已经没有这个 Agent（历史行还在）。
    status: str | None = None
    # 配置里存在 / 窗口内出现过。两个都为 False 的节点不会出现——
    # 「配置了但从没被调用」是 `configured=True, observed=False`。
    configured: bool = False
    observed: bool = False
    # 有写权限的 Agent 是架构图里最该被看清的那几个：委派链上的写能力
    # 恒等于 `min`，而图画的是**实际**发生的转交。
    can_write: bool = False
    # 第几跳（1 = 直接被叫）。`None` = 从没被委派过，是发起方或孤立节点。
    # 前端按它从左到右分层，不让图自己找布局——稳定比好看重要。
    depth: int | None = None
    # 它自己发起的对话轮次。
    traces: int = 0
    # 被转交过来的次数，**含被拒的**（数的是「有人试图交给它」）。
    #
    # 与 `/agents` 的 `delegated_turns` **不是同一个数**，别去对账：那边数的是
    # `kind="agent"` 的 span，只有子调用真的跑起来才留痕，而且 V1 之前的行
    # `agent_name` 为空（所以那些行根本不计入）。实测同一批数据里
    # ops 被转交 5 次、span 却只有 0 条——两边都对，只是问的不是同一件事。
    attempts: int = 0


class ObsTopologyEdge(APIModel):
    """一条委派边。同一对 Agent 之间可能有三条（成功 / 暂停 / 被拒），分别计数。"""

    # 发起方。`None` = 平台基线在委派（见 `ObsTopologyNode`）。
    from_agent: str | None = None
    to_agent: str
    kind: ObsEdgeKind
    count: int = 0
    last_at: datetime | None = None
    # 子调用的墙钟 P95。只有 `ok` 边有：被拒的没跑过，暂停的还没跑完，
    # 拿这两者的耗时进 P95 是编数。取自 `kind="agent"` 的 span（审计不记耗时）。
    p95_ms: int | None = None
    # 被拒的原因分布（`delegate_widens` / `AGENT_UNAVAILABLE` / `depth` …）。
    # 回答「为什么一直被挡」——只给次数不给原因，图上就只剩一个红数字。
    reasons: list[ObsBucket] = Field(default_factory=list)

    # 这条边在某个编排里被声明过。声明了却只跑出被拒的边，说明图配得比边界宽；
    # 跑过却没在任何图里，说明模型在自主路由（`ORCHESTRATION.md` §7）。
    declared: bool = False

    @field_validator("last_at")
    @classmethod
    def _utc_last(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsDeclaredEdge(APIModel):
    """编排里声明的、Agent 之间的一条边。

    `observed=False` 有两种含义，都有信息量：配了没人用（入口藏得太深），
    或者图刚建好还没跑过。判据只能是「这个窗口里跑过没有」——没有第三种。
    """

    from_agent: str | None = None
    to_agent: str
    workflows: list[str] = Field(default_factory=list)
    observed: bool = False


class ObsTopologyResponse(APIModel):
    """委派关系图：实测的委派关系 + 编排里声明的边。

    这两层是**可以互相对账**的：声明了没跑过、跑过没声明，两个方向都指向
    具体的配置问题。在此之前平台没有编排配置，硬画一张「应该是这样」等于编造；
    现在有了配置，不画反而丢掉了唯一能校验配置的信息。
    """

    hours: int
    enabled: bool
    generated_at: datetime
    scope: str = "self"
    # `self` 身份下不给图：边会暴露「别人在用哪个 Agent」，那不是排障需要的信息。
    # 返回空集而不是 403——「你没权限看这张图」与「这个接口坏了」是两件事。
    restricted: bool = False
    reason: str | None = None
    window: ObsWindow | None = None
    nodes: list[ObsTopologyNode] = Field(default_factory=list)
    edges: list[ObsTopologyEdge] = Field(default_factory=list)
    # 声明层。为空有两种可能：租户还没配流程，或窗口内一张都没被触发。
    # 前端据此决定要不要画虚线——画一层空虚线比不画更让人困惑。
    declared_edges: list[ObsDeclaredEdge] = Field(default_factory=list)
    workflow_count: int = 0

    @field_validator("generated_at")
    @classmethod
    def _utc_generated(cls, value: datetime) -> datetime:
        return as_utc(value)


# ---------------------------------------------------------------- 单次运行（L3）


class ObsOperationDelegation(APIModel):
    """一轮里的一次转交：谁把活交给谁、跑了多久、结局如何。

    取自 `kind="agent"` 的 span——它比审计多出**耗时**，比 trace 汇总多出
    「这一轮不是一个人在干」。被拒的转交不会出现在这里：子图根本没进，
    没有 span（被拒的形状在 `/topology` 上，那边读审计）。
    """

    agent: str
    # 发起方。`None` = 平台基线（没有配置 Agent 的默认助手）在委派。
    delegated_from: str | None = None
    # 第几跳。1 = 直接被叫。子图里再往下派时，那一段会另有一条。
    depth: int | None = None
    duration_ms: int | None = None
    # 子调用自己的收尾状态（`completed` / 错误分类）。与 `display_state` 不同：
    # 这里要的是「子图自己跑成什么样」，不是「这次运行该怎么给人看」。
    status: str = "ok"
    tool_calls: int = 0


class ObsOperationTrace(ObsTraceSummary):
    """运行里的一轮。在 trace 汇总之上多一段「这一轮里发生了哪些转交」。"""

    delegations: list[ObsOperationDelegation] = Field(default_factory=list)


class ObsOperationApproval(APIModel):
    """运行里的一张审批单。

    `reason` 是审批卡上那一句「要做什么」。事后回溯时它已经滚走了
    （卡片只在当轮消息里），而 `A2A.md` §9 要求复核的人看到完整一句
    「ops 在 cs 的委派下，要把工单状态改为已关闭」——运行页要把它重新拼出来。
    """

    approval_id: str
    tool_name: str
    tool_display_name: str | None = None
    risk_level: str | None = None
    # 10 个原始状态，不压成 display_state（那个另有一列）：
    # `EXECUTED` 与 `VERIFIED` 的差别正是「核验过没有」，回溯时最要紧。
    status: str
    # 执行这张单子的 Agent（不在委派链上）。
    agent_name: str | None = None
    # 直接上级的名字。`None` = 用户直接触发，不是转交来的。
    delegated_from: str | None = None
    # 整条委派链的名字，从根到直接上级。只给名字不给 id：
    # id 只能在库里对上，而这一栏是给人读的。
    delegation_chain: list[str] = Field(default_factory=list)
    reason: str = ""
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    completed_at: datetime | None = None
    # 从生成到有人处理（或过期 / 到此刻）的时长。**运行的总历时里包含了它**，
    # 两者不是并列关系——`/operations` 会把它单独扣出来成 `active_ms`。
    waited_ms: int = 0
    error_code: str | None = None
    display_state: str = "completed"

    @field_validator("created_at")
    @classmethod
    def _utc_created(cls, value: datetime) -> datetime:
        return as_utc(value)

    @field_validator("approved_at", "completed_at")
    @classmethod
    def _utc_optional(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsOperationParticipant(APIModel):
    """参与者：谁发起的、谁被叫来干活、谁把活又派出去了。

    这一栏是替审批人写的。卡在审批时刻是够的，但事后回溯时
    「谁做的决定、谁执行的」已经散在两条 trace 与一张单子里了。
    """

    agent_name: str | None = None
    display_name: str | None = None
    # initiator = 自己发起的轮次；delegate = 被叫来干活；
    # both = 又被叫又往外派。三种写法比一个布尔好读，也省得前端自己推。
    role: Literal["initiator", "delegate", "both"] = "initiator"
    traces: int = 0
    # 被转交进来几次 / 往外转交几次。两个方向分开：只给一个「交互次数」
    # 看不出这根链条往哪边流。
    delegated_in: int = 0
    delegated_out: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    # 它名下的审批单被等的时间（区间并集，不求和）。见 `ObsOperationResponse`。
    waiting_ms: int = 0

    @field_validator("first_at", "last_at")
    @classmethod
    def _utc_span(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None


class ObsOperationStep(APIModel):
    """时间线上的一格：一轮对话，或者一张要人确认的单子。

    **不带成句的标题**。措辞属于界面，接口只给结构与原始字段——
    在服务端拼中文会让同一件事出现两套说法，而改文案不该动接口。
    """

    kind: Literal["trace", "approval"]
    ref: str
    at: datetime
    state: str
    agent_name: str | None = None
    duration_ms: int | None = None
    # 只有审批步骤有：给人看的那一句「要执行什么」。
    tool_display_name: str | None = None

    @field_validator("at")
    @classmethod
    def _utc_at(cls, value: datetime) -> datetime:
        return as_utc(value)


class ObsOperationResponse(APIModel):
    """一次运行：跨轮、跨 Agent 的一条时间线。

    **三个耗时必须分开报，这是本接口存在的理由。**

    - `wall_ms`：从第一轮开始到最后一轮结束（还在等你确认的运行，到此刻为止）。
      这是「这件事从发起算起拖了多久」；
    - `waiting_ms`：其中挂在那里等人点确认的时间。**它是 `wall_ms` 的一部分，
      不是并列的另一项**；
    - `active_ms`：`wall_ms - waiting_ms`，系统真正在跑的时间。

    一次运行历时 3 小时、其中 2 小时 58 分在等人，与「系统跑了 3 小时」是
    两个完全不同的结论。只报一个数就把这条运行最值钱的信息抹掉了。
    """

    operation_id: str
    enabled: bool
    generated_at: datetime
    scope: str = "self"
    # 发起人。同一 operation 下的 trace 同属一个人（委派不换用户），
    # 取第一轮的 `user_id`。
    user_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    wall_ms: int = 0
    waiting_ms: int = 0
    active_ms: int = 0

    display_state: str = "completed"
    # 「已经结束了，但还有事没收拾」：trace 收尾了而单子还挂着。
    # 这种运行在列表里看着是完成，实际会一直占着一张待办。
    # 原因是封闭集合，措辞交给界面——同一件事不该在服务端与前端各写一遍中文。
    attention: bool = False
    attention_reason: Literal["pending_approval", "running"] | None = None

    traces: list[ObsOperationTrace] = Field(default_factory=list)
    approvals: list[ObsOperationApproval] = Field(default_factory=list)
    participants: list[ObsOperationParticipant] = Field(default_factory=list)
    steps: list[ObsOperationStep] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _utc_generated(cls, value: datetime) -> datetime:
        return as_utc(value)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_bounds(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value else None
