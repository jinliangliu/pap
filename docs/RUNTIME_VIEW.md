# 多智能体运行可视化：方案

> 目标读者：本项目维护者。本文是实施方案，不是对外文档。
> 结论先说：**可视化的单位错了。** 现在是「一轮对话 = 一条 trace」，
> 而多智能体平台的真实单位是「一次运行」——它可以包含多个 Agent、多条 trace、
> 以及以小时计的人工等待。要补的不是图表，是这一层。

与 [`OBSERVABILITY.md`](OBSERVABILITY.md) 的关系：那份文档解决「单轮内部怎么走、慢在哪」，
已经落地（M0–M3）。本文解决「**多个 Agent 一起干活时，运行是什么样**」，
复用同一套 `obs_traces` / `obs_spans`，不改那两张表的语义。

---

## 1. 现状盘点

### 已经有的

| 能力 | 落点 | 状态 |
|---|---|---|
| 一轮的 span 树（节点/模型/工具/检索） | `obs_traces` + `obs_spans` | ✅ |
| 详情页：编排流程、耗时分解、技能、工具、模型循环、答复 | `console.js mountObsDetail` | ✅ |
| 按智能体用量基准（轮次/错误/p95/token/被拒/审批/走势/环比） | `GET /observability/agents` | ✅ |
| 委派的子调用作为 `kind="agent"` 的 span 嵌在**同一条 trace** 里 | `delegate_tools.py` + `A2A.md` §10 | ✅ |
| 审批单的委派链、跨轮恢复、结论回到发起会话 | `approvals.delegation_chain` + `parent_thread_id` | ✅ |

### 缺的四件事

| # | 缺口 | 现在表现 | 后果 |
|---|---|---|---|
| 1 | **span 没有 agent 归属** | 只有 `agent.ops` 这个外层 span 带 `attrs.agent`，它下面的 `node.plan_request`、`llm.call`、`tool.call` 都不带 | 详情页的「编排流程」是一列平铺缩进，看不出哪几个节点是 ops 在跑；「模型与工具循环」把两个 Agent 的规划轮次混成一份 |
| 2 | **没有「一次运行」这个键** | 委派写操作在 `awaiting_approval` 处结束 trace，用户几小时后确认是**新 trace** | 同一个操作的两条 trace 在列表里毫无关系；`A2A.md` §10 明确拒绝 `parent_trace_id`，于是连个可查的关联都没有 |
| 3 | **收尾才写库** | `context.py` 把整棵树攒在内存，`trace_run` 的 `finally` 里一次提交 | 跑 40 秒的一轮，前 40 秒在库里完全不存在；进程被 kill 则整条丢失。**没有「现在正在发生什么」这个视图，也不可能有** |
| 4 | **三套状态词表** | trace：`ok/error/timeout/awaiting_approval/cancelled`；审批：10 个（`PENDING…EXECUTION_UNKNOWN`）；运行：`WAITING_FOR_APPROVAL/EXECUTED/VERIFIED/FAILED` | 界面上要写「这次运行现在什么样」，三个来源给三个答案。最容易出现的是「trace 已是 error，审批单还在 pending」这种组合，页面只能显示其中一个 |

---

## 2. 核心判断

**单智能体的排障问题是「这一轮慢在哪」；多智能体的运维问题是「这件事现在在谁手上、卡在哪、等了多久」。**

后者不能从前者推导出来，因为：

- 一次运行的边界不等于一轮对话——委派写操作天然横跨两次 HTTP 请求；
- 「卡住」不等于「出错」——`input-required` 是**暂停**，不是失败，但它在 trace 里长得像「这一轮很快就结束了」；
- 「谁做的」不等于「谁的会话」——`obs_traces.agent_name` 记的是发起方，被委派方只出现在 span 的 attrs 里。

所以本文的四个视图按**海拔**分层，而不是按功能分模块：

```
L0  现在          谁在跑、谁在等人工、等了多久          秒级   运维值守
L1  平台          谁在被用、谁在错、谁被挡             天级   取舍与配额
L2  拓扑          智能体之间真实的委派关系             周级   架构与边界
L3  单次运行      这一次请求在整个网络里怎么走的        单次   排障
```

L1 已有（`/overview` + `/agents`）。本文补 L0、L2、L3，并给 L1 补两个列。

---

## 3. 行业对照：取什么、不取什么

2026 年这块已经收敛了。逐个说清借鉴哪一条，以及为什么有些看着很像的东西不能照搬。

| 行业做法 | 它的答案 | 本项目取什么 |
|---|---|---|
| **OpenTelemetry GenAI 语义约定**（v1.41，CNCF GenAI SIG）<br>`invoke_agent {name}` / `execute_tool {name}` / `chat` / `plan`，属性 `gen_ai.agent.id/name`、`gen_ai.conversation.id`、`gen_ai.usage.*`。<br>多智能体的规定很直白：A 调 B 时，**B 的 `invoke_agent` 是 A 某个 span 的子节点** | 这就是本项目 `kind="agent"` 已经在做的事 | **结构不动，只对齐命名。** 现在 `kind` 是自有词表（`run/graph/node/llm/tool/mcp/db/retrieval/approval/agent`），导出 OTLP 时做一层映射即可。不为了「标准」去改库里的值——那要迁移、要改测试、要改前端，换来的只是名字 |
| **A2A 协议的 TaskState**：`submitted / working / input-required / auth-required / completed / failed / rejected / canceled`，其中 `input-required` 与 `auth-required` 明确标注**非终态、暂停** | 长时运行的状态机，且把「等人」当成一等状态 | **借它的词表，不借它的实现。** 我们的 `awaiting_approval` 就是它的 `input-required`。统一在**出口**（接口返回一个 `display_state`）派生，不改库里的枚举值 |
| **Temporal Web UI**：workflow 的 event history + 「pending activities」，卡在某个 activity 上一眼可见，可以「看它现在停在哪一步」 | 持久化执行的可视化；这正是审批跨小时要做的事 | **取「暂停点可见」这个要求**，不取事件溯源（我们的库已经是事实表，不需要 replay） |
| **Grafana Tempo / Jaeger 的 service graph**：服务图由 span 的父子关系**推导**，不是从配置读的 | 拓扑应该反映事实 | **同法推导。** 实测层永远由 span 推导；声明层（编排里配的边）只作虚线对照，**不参与推导**——`ORCHESTRATION.md` 上线后仍守这条：画「应该怎样」不等于把它当成事实。配了但从没被调用的 Agent 单独列出——那是「建了没人用」的信号 |
| **Langfuse sessions / LangSmith threads**：把多轮归到一个 session 下看 | 会话级时间线 | **只取「分组」这一层。** 我们有 `conversation_id`（会话）和 `trace_id`（一轮），缺的正是中间那层「一次运行」 |
| **内容捕获默认关、按环境 opt-in** | 隐私默认值 | 已有（`OBS_CAPTURE_CONTENT`），不动 |

不取的还有两个，写在这里免得以后被当成遗漏：

- **不做 eval / 评分层。** 那是另一条链路（数据集、评分器、回归），混进运行可视化会把「他在干什么」和「他干得好不好」搅在一起，两件事的可信度都会下降。
- **不做前端框架与图形库。** 现有约束是无框架、DOM+CSS（`OBSERVABILITY.md` §9）。拓扑图用 SVG 手绘，节点数是个位数到几十，不值得引 D3。

---

## 4. 数据层：三处改动

### 4.1 `obs_spans.agent_name`（新增列 + 索引）

```sql
ALTER TABLE obs_spans ADD COLUMN agent_name varchar(64) NULL;
CREATE INDEX ix_obs_spans_agent_started ON obs_spans (agent_name, started_at);
```

**为什么成列而不是留在 attrs 里。** 拓扑聚合是 `GROUP BY`，每刷新一次就扫一遍 JSON；
`->>` 与 `json_extract` 不通用（`OBSERVABILITY.md` 已经为这个吃过一次亏，`p95` 就是这么被迫用 Python 算的）。
这与既有先例一致：`tool_name` / `skill_name` / `agent_name` 在 `obs_traces` 上成列，
理由原文就是「列表页要按它们筛，扫 JSON 的代价比多一列大得多」。

写入侧：在 `context.py` 加 `bind_agent(name)` 上下文管理器，`span()` 创建记录时读当前值。
委派子调用进入子图前 `bind_agent(target)`，退出时还原。嵌套委派 `A→B→C` 时，
C 的 span 归 C——`delegation_ancestors` 已经在 context 里，顺序天然正确。

存量行 `agent_name` 为 NULL，与「平台基线（没有配置 Agent）」的 NULL 无法区分。
不回溯：解释口径写进接口注释——**NULL 一律按「未归属」处理，不当作基线**。

### 4.2 `obs_traces.operation_id`（新增列 + 索引）

一次运行可能对应多条 trace。用一个**扁平分组键**把它们串起来：

```sql
ALTER TABLE obs_traces ADD COLUMN operation_id varchar(32) NULL;
CREATE INDEX ix_obs_traces_operation ON obs_traces (operation_id, started_at);
```

规则：开新 trace 时若没有可继承的值，`operation_id = trace_id`（自己就是这条运行的起点）；
`resume_approval` 从审批单上取 `operation_id` 继承。于是「一次运行」= 同一 `operation_id` 的 trace 集合，
且第一轮不会因为「没有父」而特殊处理。

**这为什么不是把 `A2A.md` §10 拒绝的 `parent_trace_id` 换个名字。** 那条决策的理由是：
一旦 trace 之间有父子边，所有求和口径会失效——`by_agent` 之和不再等于 `traces`，
概览分布图和总数对不上。那个理由成立的前提是**它是一棵树**。

`operation_id` 是**分组键，不是边**：同一组内所有成员的值相同（含第一轮），没有任何「父」。
它不能被用来做 rollup——想 rollup 得先构造父子关系，而这里没有。
口径保护的写法是一条测试：`sum(按 agent 的 traces) == total_traces`，加了这个列之后必须仍然成立。

> 若以后有人真想按运行汇总，正确做法是显式查询 `GROUP BY operation_id` 并在界面上标明
> 「这是运行数，不是轮次」，而不是让 `traces` 这个数自己变。

### 4.3 状态词表：在出口派生，不改库

新增一个纯函数（`app/observability/state.py`）：

```python
def display_state(*, trace_status, approval_status, run_status) -> str:
    """把三套词表压成一个界面状态，用 A2A TaskState 的措辞。"""
```

| 派生结果 | 判定顺序（先命中先用） | 出处 |
|---|---|---|
| `rejected` | ① `DELEGATION_REVOKED` / `AGENT_UNAVAILABLE` / `AGENT_BOUNDARY_CHANGED` | trace.error_category |
| `failed` | ② trace `error` / `timeout` / `abandoned`；③ 审批 `EXECUTION_UNKNOWN` / `RECOVERY_REQUIRED` | 两份来源 |
| `input-required` | ④ 有 pending 审批单 | `approvals.status = PENDING` |
| `working` | ⑤ trace 行存在且未收尾 | §5 的 live 行 |
| `canceled` | ⑥ trace `cancelled` / 审批 `REJECTED` / `EXPIRED` | 两份来源 |
| `completed` | ⑦ 其余（`EXECUTED` 与 `VERIFIED` 在这里同档，强弱另有一列） | 两份来源 |

判定顺序不是随手排的，三条都有具体反例：

1. **否决优先于失败。** `DELEGATION_REVOKED` 会让 trace 落成 error，但那时 B 可能完全健康，
   被收窄的是上游。显示成 `failed` 会把运维引去查一个没问题的执行方。
   实机上 SA38（批准期间上游收窄）就是这一类。
2. **失败优先于等待。** 子调用崩在审批之后时，库里会同时有「trace 已 error」与「单子还 PENDING」。
   这不叫等待——没有人会去恢复它。显示成 `input-required` 会让人以为「点一下确认就好了」。
3. **等待优先于在跑。** 一个既有 pending 单子又在跑的运行，对运维来说「有人在等」更急。

> 这三条里第一条最容易写反。实现时就是这样：先前把「trace 已 error」放在最前面，
> 单测立刻在 `DELEGATION_REVOKED` 上变红——因为复核对被否掉的那条，trace 一定是 error。

由此产生一个**必须单独暴露的信号**：`needs_attention`——运行已终态，但审批单还挂着
（或反过来）。这种运行在列表里看起来是「完成」，实际留了一张没人管的单子，
不挑出来就会在库里躺到过期。

派生而不落库的理由：三套状态各自有生命周期和写入方，任何一处改成「写一个统一的 status 列」
都会立刻出现两个真相。出口派生是唯一不会漂的做法。

---

## 5. L0「现在」：让运行中的东西可见

这是**唯一需要改动写入链路**的一处，也是最值钱的一处。

### 5.1 开始即落一行

`trace_run` 进入时提交一条 `status="running"` 的 trace 行（同上队列），收尾时 UPDATE 成最终状态。
`ObsWriter` 的队列消息从 `{tenant_id, trace, spans}` 扩成带 `type` 的两种：`start` / `finish`。

代价必须写在脸上：

| 变化 | 代价 |
|---|---|
| 每轮从「1 次批量 INSERT」变成「1 次 INSERT + 1 次 UPDATE」 | 写入量 +1 次，仍在 1 秒攒批内，可忽略 |
| `context.py` 的「整棵树一起提交」约束被打破 | 只打破一半：span 仍然整棵提交，只是 trace 行提前存在 |
| 进程被 kill 会留下永久 `running` 的残行 | **必须配 sweeper**（下条），否则「在跑」永远说谎 |

采样与开关的交互：`OBS_SAMPLE_RATE < 1` 时**不写 start 行**（否则被采样丢弃的成功 trace 会留下只在开头出现过的残行）。
即：start 行只在「必定保留」时写。这条会写成测试。

### 5.2 残行清扫

`expiry_service` 已有的定时任务里加一步：`status="running"` 且 `started_at` 超过
`OBS_RUNNING_TTL`（默认 30 分钟，超过单轮最长耗时一个量级）的行，
改成 `status="abandoned"`，`error_category="orphaned_running"`。

不是删除：**「有一轮跑到一半进程没了」本身就是需要被看见的事实**，删掉等于把事故抹平。

### 5.3 `GET /observability/live`

一个接口回答「现在怎么了」，返回两组：

- `running`：未收尾的 trace，带 `age_ms`、`agent_name`；
- `waiting`：`PENDING` 的审批单，带 `waited_ms`、`agent_name`、`delegated_from`、`operation_id`、`expires_at`。

按 `waited_ms` / `age_ms` 倒序。超过阈值（默认 15 分钟）的标 `stale=true`——
「有人在等一个已经没人管的审批」是这张视图存在的主要理由。

**实现后改了一处设计：不返回「当前已有 span 数」。** 上一版写的是「带当前已有
span 数，说明它跑到哪一步」，但 §5.1 只打破了「整棵树一起提交」的一半：
trace 行提前落库，span 仍然攒到收尾。所以运行中的 trace 一条 span 都没有，
那个数恒为 0——写出来会被读成「它还没开始干活」，比不写更误导。要看跑到哪一步，
只能等这轮结束。运行视图能回答的是「跑了多久、谁在跑」。

前端：观测页顶部一条常驻横条，只在有内容时出现（没有在跑、没有在等时整条不渲染，
不留一个「0 个」的空壳）。5 秒轮询，切页即停。

> 实测（2026-11-12）：发一轮正常对话，t=2s / t=3s 时 `/live` 的 `running` 里能看到
> `assistant`、`age_ms` 2022 / 3283，t=4s 消失——收尾 UPDATE 把它变成了 `ok`。
> 这说明起始行、收尾、`/live` 三者在真实链路上都通了，不是只在测试里通。

---

## 6. L2 拓扑：委派关系图

### 6.1 数据

**实现后改了一处设计：边来自审计（`agent_delegated`），不是 `kind="agent"` 的 span。**

原计划按 span 推边（`(父链上的 agent_name, span.agent_name)`），动手时撞上两处硬伤：

1. **被拒的转交根本没有 span。** 深度 / 环 / 预算 / 边界 / 引用五种拒绝全部发生在
   `agent.{target}` 创建**之前**（`delegate_tools.execute` 的早退分支），于是
   「一直被拒」——最需要被看见的那条边——在数据里不存在。
2. **暂停的转交在 span 上写着 `completed`。** 子图停在审批时 `ainvoke` 正常返回、
   `state["error_category"]` 为空，`handle.add(status=...)` 便记下 `completed`；
   而 `SpanHandle.add` 写的是 **attrs**，连 span 自己的 `status` 列都没动过。
   「等人工」与「跑完了」在 span 上长得一模一样。

审计侧一条 `agent_delegated` 就是一次转交尝试，`metadata.status` 明确是
`completed / awaiting_approval / rejected / failed`，四种结局分得开；而且它不受
保留期影响——拓扑问的是周级问题，14 天后 trace 被清了，这条关系还得在。
span 只补一个数：`ok` 边的 P95 墙钟（审计不记耗时）。

父是谁也不再需要沿 `parent_span_id` 往上找：审计里直接记了 `from_agent`。
`from_agent` 为空表示**平台基线**（没配 Agent 的默认助手）在委派——它是图上一个
独立节点，不是「没有来源」。

节点集合 = 观测到的 Agent ∪ **已配置的 Agent**。后者里窗口内零活动的那些
`configured=true, observed=false`，画在**图外**单独一行、灰显、标注「配置了，
窗口内一次都没被调用」，不带边。这一类的价值高于图本身：它回答「我建的这个
Agent 到底有没有用」。混进图里会变成一堆没有边的散点，所以前端把它们放成图下的
标签行，而不是硬塞进分层里。

### 6.2 四种边

| 边 | 含义 | 画法 |
|---|---|---|
| 成功 `ok` | 子集校验通过且子调用走完 | 绿色实线，粗细按次数 |
| 暂停 `paused` | 子调用停在审批（`awaiting_approval`） | 橙色虚线，标「等人工」 |
| 被拒 `rejected` | 边界 / 目标不可用 / 深度 / 预算挡下，**子图没进** | 红色虚线，附原因分布 |
| 失败 `failed` | 转交成立，但子调用崩了 / 超时 / 无证据结论 | 灰色点线 |

原先写的是三种。「失败」是第四种，加它是因为审计侧本来就有这个取值：丢掉它，
「被接受的转交次数」与图上各边之和就对不上，而一个加不平的图会让人怀疑整张图。

四种必须分开：合并成一条「调用失败」会让人去修一个配得好好的边界。
**「一直被拒」和「一直跑通」是两种完全不同的架构决策**。

### 6.3 `GET /observability/topology`

返回 `nodes[]`（`name` / `display_name` / `status` / `configured` / `observed` /
`can_write` / `depth` / `traces` / `attempts`）与 `edges[]`
（`from_agent` / `to_agent` / `kind` / `count` / `last_at` / `p95_ms` / `reasons`）。
不返回布局坐标——前端算，后端不管画法。

`name` / `from_agent` **可以为 null**：那是平台基线。用 `null` 而不是塞一个占位
字符串，因为 `Agent.name` 只校验长度、不校验字符集，真有人可以建一个叫 `—` 的
Agent——而「没有 Agent」与「一个名字很怪的 Agent」在排障时不是一回事。

节点上的 `attempts` 数的是「有人试图交给它」的次数，**含被拒的**。

它与 `/agents` 的 `delegated_turns` **不是同一个数**，两张表不要互相对账：
后者数 `kind="agent"` 的 span（只有子调用真的跑起来才留痕，而且 V1 之前的行
`agent_name` 为空、根本不计入）。实测同一批数据里 `ops` 被转交 5 次、span 只有
0 条——两边都对，只是问的不是同一件事。所以这里换了个名字：同名的两个数放在
两张表上，迟早有人拿去做减法。

`p95_ms` 只给 `ok` 边。取值时先看 span 的 `agent_name`，为空则回退 `attrs.agent`
——V1 之前的委派行只有后者，不回退的话那几条边的耗时永远是空的。

`reasons` 只给被拒边，最多三条：回答「为什么一直被挡」。只给次数不给原因，
图上就只剩一个红数字。

前端：SVG 手绘，力导向太贵也没必要。规则简单固定：**发起方在左、被委派方在右，
按 `depth` 分层**，同层先按调用量垂直排，再按「父节点的平均行号」做**一次**
重心调整。这与 `A2A.md` 的「单调收窄」语义一致（箭头方向就是权限流向），
也不会因为数据抖动让图自己跳舞。同一对节点之间的多条边扇开几像素，
否则会叠成一条线、看起来像「只有一种结局」。

扇开只解决了线，没解决**线上的数字**：三条边的数字都摆在中点时只隔一个扇开量，
10.5px 的字会糊成「5 上面压着 2」。所以数字沿各自的曲线错开取值（`t` 从 0.3 到 0.7），
并且跟着边类型上色——不然一束里的「5」看不出是跑通还是被拒，标注等于没有。
`scripts/check_render.mjs` 盯着这条：同一束数字的横坐标必须互不相同。

重心只做一次、不迭代：多次迭代会让同一份数据在「看起来差不多」的两个解之间跳，
而这张图的价值恰恰是**刷新两次位置一样**。

图按 1:1 出（`width` 属性 = 分层算出的像素），不用 `width:100%` 拉伸——
拉伸会把 12.5px 的字放大到二十几号，跟页面上其余文字对不上，看起来像「被放大了」
而不是「画得清楚」。图比卡片窄时居中，窄屏时按比例缩。

---

## 7. L3 单次运行：跨轮、跨 Agent 的一条时间线

`GET /observability/operations/{operation_id}` 返回：

- 该 `operation_id` 下的全部 trace（按时间），每条带 `display_state`；
- 关联的审批单（含委派链、等待时长、确认人、执行结果）；
- 每条 trace 的 agent span 摘要（`agent` / `depth` / `delegated_from` / 耗时 / 工具数）；
- 合并后的总耗时 `wall_ms`、「其中等人工」`waiting_ms`、`active_ms`（= 两者之差）。

**两个数字必须分开。** 一次运行总耗时 3 小时，其中 2 小时 58 分在等人，
这和「系统跑了 3 小时」是完全不同的结论。合并成一个数就把这条运行最重要的信息抹掉了。

`waiting_ms` 按**区间并集**算，不求和：两张审批单同时挂着时求和会超过 `wall_ms`，
而「等了两倍时长」根本不是事实。区间再裁剪到窗口内（`clipped_start = max(wait_start, start)`），
否则一张跨窗口挂着的单子会把整段历史都算进这个窗口。

`display_state` 是聚合结果而不是某一轮的照抄：**trace 都收尾了而审批单还挂着**时给
`input-required` + `attention=true` + `attention_reason="pending_approval"`，`wall_ms`
算到此刻。不这么聚合，那次运行在列表里看着是「完成」，实际一直占着一张待办，
没人会点开一个已完成的东西。

### 7.1 泳道视图

trace 详情页的「编排流程」在**存在 `kind="agent"` 且 `depth>=1` 的 span** 时，卡头多一对
「流程 / 泳道」开关：每个 Agent 一行，横轴时间，横条是这名 Agent 活跃的那一段，
委派是一次跨行的虚线，失败点单独戳一根竖线（横条只说明「在干活」，不说明「干砸了」）。

不改单 Agent 的情况：缩进盒子列表在这个规模上比泳道好读，没有委派的 trace 保持现状，
连开关都不给——点过去只会看到一条通到底的横条。
**视图形态跟着数据形态走，不搞「统一成一种」**。

切到泳道时那张卡独占一行：泳道图是 642px 定宽的，塞进半栏会等比缩到七八成，
10.5px 的轴标签跟着糊掉。切换只动 DOM，不重新拉接口，也不收起已经展开的 span 原文。

### 7.2 单次运行的页面结构

```
这一次运行   历时 / 其中等人工 / 实际在跑 / 轮次 / 审批单 + 一条等待占比条
这一次是怎么走的  第 1 轮（发起）→ 等确认 → 第 2 轮（确认后执行），点任一行进 trace 详情
参与者       谁做的决定、谁干的活：自己的轮次 / 被转交进来 / 转交出去 / 它名下的等待
审批单       卡上那句话 + 完整委派链 + 谁批的 + 原始状态（EXECUTED / VERIFIED 不归一）
```

入口只有一个：trace 详情页右上角的「这一次运行」。不在列表上加列——九成运行只有一轮，
那样每行都会多一个「和本行内容一样」的按钮。从运行页点进某一轮，返回时回到这条运行
而不是一路弹回列表（`obs.operation` 保留、`obs.detail` 清空）。

「产物」那一栏（单据 / 附件 / refs）还没做：`refs` 目前只在单轮详情里有，
跨轮合并要先定「哪些引用算这次运行的产物」，否则拼出来的是四轮引用的并集，读不出归属。

「参与者」这一栏是替审批人写的：`A2A.md` §9 要求审批人看到完整一句
「ops 在 cs 的委派下，要把工单状态改为已关闭」。那张卡在审批时刻是够的，
但事后回溯时那句文案已经滚走了，所以运行页要把它重新拼出来。

---

## 8. 接口一览

| 方法 | 路径 | 用途 | 新增 |
|---|---|---|---|
| GET | `/observability/live` | 在跑 + 在等 | ✅ |
| GET | `/observability/topology?hours=` | 节点与边 | ✅ |
| GET | `/observability/operations/{id}` | 一次运行的合并视图 | ✅ |
| GET | `/observability/traces` | 加筛选 `operation_id`、`display_state`；每行带 `display_state` | 改 |
| GET | `/observability/traces/{id}` | span 带 `agent_name`；有委派时返回 `operation_id` | 改 |
| GET | `/observability/agents` | 加 `delegated_turns`（✅ 随 P3-b 先行落地）、`waiting_ms`、`delegated_to[]` | 改 |

口径三条，与既有约束一致：

1. 可见范围仍按 `_scope_of()` 收：`self` 身份只看得到自己发起的那部分。
   拓扑在 `self` 下**不给**——边会把「别人在用哪个 Agent」泄露出去，那不是排障需要的信息。
2. `operation_id` 不是权限边界，只用于分组。拿到别人的 id 查运行 → 404。
3. `delegated_turns` 与 `traces` **两个数不能相加**：前者是「被叫去做事的次数」，
   后者是「发起的轮次」。它们的分母不同，界面上分列，不做合计行。

---

## 9. 明确不做

| 事项 | 理由 |
|---|---|
| 实时推送（WebSocket / SSE） | `OBSERVABILITY.md` 已定 5 秒轮询；运行可视化的时效要求是「一分钟内知道」，不是「实时」 |
| 通用服务拓扑（DB、MCP server、模型端点都画进去） | 那是 APM 的活。这里只看 Agent 这一层，多画一层就多一层没人维护 |
| 配置侧的「应该是这样」拓扑 | 没有编排配置时硬画等于编造。现在有了声明层（`ORCHESTRATION.md`）：声明虚线、实测实线，不一致的边两个方向都有信息量 |
| 甘特 / 瀑布图的通用组件 | 现有详情页的耗时分解够用；泳道是唯一新增形态 |
| 运行重放 / replay | 事件溯源是 Temporal 的路子，我们的库是事实表。想看过程就打开那条 trace |
| 跨租户拓扑 | 租户是硬边界，委派本身就禁止跨租户 |
| 拓扑的自动布局调参 | 固定分层（左→右，按 depth）已能表达语义，力导向的抖动只会让人怀疑数据 |

---

## 10. 分期

| 期 | 范围 | 产出 | 状态 |
|---|---|---|---|
| **V1** | `obs_spans.agent_name` + `bind_agent()` + `operation_id` + `display_state()` + 迁移 | 数据基座，接口先带出新字段 | ✅ |
| **V2** | trace 开始落行 + 残行清扫 + `/live` + 顶部横条 | 「现在谁在跑、谁在等」 | ✅ `753fad0` |
| **V3** | `/topology` + SVG 委派图 | 「委派关系长什么样」 | ✅ |
| **V4** | `/operations/{id}` + 泳道视图 + `agents` 的 `waiting_ms` / `delegated_to[]`（`delegated_turns` 已由 P3-b 提前做完） | 「这一次到底怎么走的」 | ✅ `3863250` |
| **V5** | 等待超时接 watchdog；`operation_id` 进审计 | 运维闭环 | 未开始 |

顺序不能换：V2 依赖 V1 的 `display_state`，V4 依赖 V1 的 `agent_name`。
V1 单独上线也没有副作用（只多两列，前端不用改）。

V2 实现时多定了三件文档里没写的事：

1. **同批 start+finish 要合并。** 1 秒内跑完的一轮会同时落进同一批，分开写就是
   先 INSERT 再 UPDATE，中间态还会被别处的查询读到。按 `trace_id` 合并，只留最终态。
2. **收尾不能用 `session.merge()`。** merge 靠主键 `id`（uuid）定位，而起始行与
   收尾行各自生成一个 uuid，它永远找不到已存在的那行，只会 INSERT 并撞
   `trace_id` 的唯一约束。按业务键 UPDATE、未命中再 INSERT 才是这里要的语义。
3. **`running` 不计入错误率。** 它没有结论，既不是成功也不是失败。原先
   `status != "ok"` 一律算错误，于是每个刚点发送的轮次都让错误率跳一下——
   那是最容易让人不再相信这个数字的方式。`abandoned` 仍然算：那是事故。
   同一条理由后来也适用于 `awaiting_approval`（V3 一并改掉）：它同样是暂停，
   而「每一次正常的写操作都记一笔失败」会让有写操作的系统常年三成以上错误率。
   口径表与分界日见 `OBSERVABILITY.md` §8.1。

V3（委派拓扑）实现时定了两件文档里没写的事：

1. **边来自审计，不是 span。** 两处硬伤见 §6.1；改完之后，被拒的边从「不存在」
   变成「有原因分布」的那一列——它恰好是这张图最值钱的信息。
2. **有节点、没有边时不画空画布。** 空白画布看起来像「图没加载出来」，而它实际是
   「这段时间里没有任何转交」这个明确结论。空状态里写明「调大时间窗」，
   并保留图外的孤立节点清单——那部分信息与边无关，不该被一起收走。

---

## 11. 验收（每条都要能反向验证）

前六条会写成测试。**去掉实现必须变红**——只测成功路径的用例在这块没有价值，
因为最难发现的错误是「图看起来对，但边连错了人」。

1. **归属正确**：`A→B` 一轮，B 的 `node.plan_request` span 的 `agent_name` 是 `B`，
   A 的是 `A`；`A→B→C` 时 C 的 span 归 C。
2. **口径守恒**：加 `operation_id` 后，`sum(by_agent.traces) == total_traces` 仍成立（这是
   `A2A.md` §10 拒绝 `parent_trace_id` 的原始理由，必须被钉住）。
3. **运行串起来**：委派写 → 确认，两条 trace 的 `operation_id` 相同且等于第一条的 `trace_id`。
4. **状态不打架**：构造「审批 pending 且 trace 已 error」，`display_state` 必须是 `failed`
   且文案提示「单子还挂着」；只返回 `input-required` 算不通过。
5. **残行必被清**：把 `started_at` 改到 1 小时前且 `status=running`，清扫后变 `abandoned`，
   **且行还在**（不是删除）。✅ `test_obs_live.py::test_orphaned_running_is_marked_not_deleted`
6. **采样不放残行**：`OBS_SAMPLE_RATE=0` 时，start 行一条都不写。
   ✅ `test_obs_live.py::test_sampled_out_trace_leaves_no_running_row`
7. **拓扑只画事实**：配一个从没被调用的 Agent → 出现在孤立节点里，不带边；
   构造一次 `delegate_widens` → 出现一条「被拒」边而不是「成功」边。
   ✅ `test_obs_topology.py::test_topology_lists_configured_agent_without_edges`
   ＋ `::test_topology_separates_success_paused_and_rejected`
8. **两个耗时分开**：等人工 2 分钟的一次运行，`waiting_ms ≈ 120000` 且 `wall_ms > waiting_ms`，
   不允许 `wall_ms` 把等待算进「系统耗时」。
9. **权限**：`self` 身份请求 `/topology` → 返回空节点集而非 403，界面显示「需要 observability:read」。
   ✅ `test_obs_topology.py::test_topology_returns_empty_for_self_scope`
10. **关掉观测**：在控制台「运行配置 → 观测」关掉时，`/live` 返回 `enabled=false` + 空列表，
    不 404，不建表也不写行。

---

## 12. 风险

| 风险 | 应对 |
|---|---|
| `running` 残行堆积，让「在跑」不可信 | 清扫任务 + `stale` 标记 + 界面区分「在跑」与「疑似已死」 |
| 拓扑被误读成「允许的关系」 | 图上标注「来自观测到的调用，不是配置」；被拒边单独成色 |
| `agent_name` 大量为 NULL（存量数据） | 不回溯；界面按「未归属」展示，不混进基线 |
| 两个耗时口径被合并（wall / waiting） | 分别成列，标签写全称，不做「总耗时」这种含糊列名 |
| 泳道在 3 个以上 Agent 时挤成一团 | 超过 4 个 Agent 时默认折叠为「按 Agent 分组的列表」，泳道作为可选视图 |
| 新增 5 个查询把观测库压垮 | 全部走 `obs_*` 已有索引 + 新增两条；`/live` 只扫未收尾行（数量天然少） |
