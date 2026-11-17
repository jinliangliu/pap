# 自研智能体观测（Observability）规划

> 目标读者：本项目维护者。本文是实施方案，不是对外文档。
> 结论先说：**退役 LangSmith，改为进程内埋点 + 落库 + 控制台「观测」页**。
> 观测数据全部留在自己的库里，按租户隔离，能回答「这一轮慢在哪、错在哪、
> 花了多少 token」，而不是只能看一条外部托管的调用链。
>
> **定位（2026-11-05 确认）**：观测是**开发阶段的调试手段**，正式对外运行前关闭。
> 因此默认策略是「生产环境默认关、开发环境显式开」，关闭后必须零开销、零写入、
> 零外发（含 OTLP）。开关与上线前检查清单见 §6.1 与 §11。
>
> **三项已拍板**（原先 §15 的三个问题）：保留期 14 天；**记录完整原文**；
> 保留 OTLP 出口。这三条决定了 §4 的字段、§7 的脱敏策略与 §12 的 M5 范围。
>
> **本文只管单轮内部**（「这一轮慢在哪」）。多智能体一起干活时的运行视图——
> 「现在谁在跑 / 谁在等人工 / 委派关系长什么样 / 一次运行横跨几条 trace」——
> 见 [`RUNTIME_VIEW.md`](RUNTIME_VIEW.md)。那边复用同一套两张表，只在出口做派生。

## 1. 目标与非目标

### 要解决的四类问题

| 问题 | 现在的答案 | 应有的答案 |
|---|---|---|
| 这轮对话为什么慢 | 翻 journalctl 里的零散 `event=` 日志 | 一条 trace，按阶段拆开：技能/工具/循环各自耗时，一眼看出是模型慢还是工具慢 |
| 为什么答错了 | 猜，或者复现 | trace 上带意图、工具名、参数摘要、工具返回摘要、错误归类 |
| 这个月用量多少 | 不知道 | 按租户 / 用户 / 意图 / 工具统计调用数、token、错误率 |
| 某个用户投诉的是哪一轮 | 用 thread_id 去库里捞消息 | 从会话直接跳到那一轮的 trace |

### 非目标（明确不做）

- **不做通用 APM**：不采集 SQL 语句、不抓函数级 profile、不做分布式追踪的服务拓扑。
  OTel 那套保留为可选出口，但本方案不依赖它。
- **不替换 watchdog**：主机/进程/磁盘的存活巡检仍由 `scripts/watchdog.py` 负责；
  观测只提供「业务链路」这一层的指标，M4 才把错误率喂给 watchdog。
- **不做实时流式看板**：列表 5 秒轮询即可，不上 WebSocket。
- **不接第三方托管**：数据不出本机（这是退役 Smith 的初衷）。

## 2. 现状盘点

**LangSmith 目前怎么用的**：没有任何代码引用，纯靠环境变量开启——
`LANGCHAIN_TRACING_V2` / `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` /
`LANGSMITH_PROJECT` / `LANGSMITH_ENDPOINT`（见 `.env.example:165-174`）。
langchain-core 自动给图节点、模型调用挂 callback，把 trace 推到
`api.smith.langchain.com`。所以退役成本很低：**删变量即可，不用动业务代码**。

**已经有的、可以直接复用的底座**：

| 模块 | 内容 | 与本方案的关系 |
|---|---|---|
| `agent_runs` 表 | 每轮一行：status / intent / tool_name / approval_id / input_summary / output_summary / error_category / started_at / finished_at | 保留为**业务事实表**（谁、何时、结果如何）；观测表是它的**诊断明细**，两者用 `run_id` 关联 |
| `messages.metadata_json` | intent / tool_name / ocr_text | 不动。观测不复刻消息内容 |
| `app/observability/metrics.py` | 进程内计数 + 平均耗时，`/metrics` 文本输出 | 保留；但 nginx 目前把 `/metrics` 返回 404，且只有进程级粗粒度，无法按 trace 下钻 |
| `app/observability/tracing.py` | OTel SDK + FastAPI/httpx 自动埋点，未配 `OTEL_EXPORTER_OTLP_ENDPOINT` 时静默 | 保留为可选出口；M5 可把自研 span 桥接过去，不重写 |
| `audit_logs` | 追加式安全审计（登录、权限、工具审批） | 语义不同（合规），不要拿来当 trace 用 |
| 图节点 | begin_turn / plan_request / execute_read_tool / retrieve_knowledge / create_and_wait_approval / respond / persist_state | 每个节点天然是一个 span 边界 |
| 工具网关 | `app/tools/gateway.py` 的 `execute_read_only` / `execute_approved` | 工具调用的**唯一**埋点位置 |

**缺口**：没有 token 用量、没有单轮耗时的分段时间、没有错误分组、没有工具参数/返回摘要、
没有可下钻的界面。LangSmith 能补一部分，但它的数据模型里没有「租户 / 站点 / 审批」
这些业务语义，界面也不认本项目的意图分类，所以补得不彻底。

## 3. 方案总览

```
HTTP /api/v1/chat
  └─ run  (trace 根)                        ← chat_service._handle_message
      ├─ persist.user_message               (db)
      ├─ graph.invoke                       (graph)
      │   ├─ begin_turn                     (node)
      │   ├─ plan_request                   (node)
      │   │   └─ llm.call                   (llm, 1..n 次，含 reasoning_effort 重试)
      │   ├─ execute_read_tool              (node)
      │   │   └─ tool.call                  (tool)
      │   │       └─ mcp.request            (mcp, 远端时)
      │   ├─ retrieve_knowledge             (node)
      │   │   └─ retrieval.search           (retrieval)
      │   ├─ create_and_wait_approval       (node, 写操作时)
      │   └─ respond                        (node)
      │       └─ llm.call                   (llm, 若需要格式化)
      └─ persist.assistant_message          (db)
```

一条用户消息 = 一个 trace；每个节点/外部调用 = 一个 span；两者都带
`trace_id`，span 用 `parent_span_id` 组成树。前端按阶段聚合成卡片，用树画编排流程。

**ID 直接用 OTel 规范的格式**（trace 32 位 hex / span 16 位 hex）。不引入 OTel SDK
也能生成，但将来若要导出到 Tempo/Jaeger，字段不用改。这是个便宜的向后兼容。

## 4. 数据模型

两张表。为什么不并成一张：列表页只查 trace（过滤 + 分页），详情页才查 span；
保留清理按 trace 级联删除。分开写，查询与清理都简单，代价只是每轮多一次 UPDATE
（写汇总字段）。

```sql
CREATE TABLE obs_traces (
    id              uuid PRIMARY KEY,
    trace_id        varchar(32)  NOT NULL UNIQUE,
    tenant_id       varchar(128) NOT NULL,
    user_id         varchar(128) NOT NULL,
    conversation_id uuid NULL,
    run_id          varchar(64)  NULL,        -- 关联 agent_runs.id，两表不建外键
    thread_id       varchar(255) NULL,

    started_at      timestamptz  NOT NULL,
    ended_at        timestamptz  NULL,
    duration_ms     integer      NULL,

    -- ok | error | timeout | awaiting_approval | cancelled
    status          varchar(24)  NOT NULL,
    error_category  varchar(64)  NULL,
    intent          varchar(64)  NULL,
    -- 主工具与命中的技能单列成列而不是放 attrs：列表页要按它们筛，
    -- 扫 JSON 的代价比多一列大得多。
    tool_name       varchar(64)  NULL,
    skill_name      varchar(64)  NULL,

    span_count      integer      NOT NULL DEFAULT 0,
    llm_calls       integer      NOT NULL DEFAULT 0,
    tool_calls      integer      NOT NULL DEFAULT 0,
    prompt_tokens   integer      NULL,
    completion_tokens integer    NULL,
    cost_micros     bigint       NULL,        -- 未配单价时为 NULL，不写 0

    input_text      text         NULL,        -- 用户原文（full 模式完整记录），见 §7
    output_text     text         NULL,        -- 最终回答原文
    attrs           jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX ix_obs_traces_tenant_started ON obs_traces (tenant_id, started_at DESC);
CREATE INDEX ix_obs_traces_status_started ON obs_traces (status, started_at DESC);
CREATE INDEX ix_obs_traces_intent_started ON obs_traces (intent, started_at DESC);
CREATE INDEX ix_obs_traces_conv ON obs_traces (conversation_id, started_at DESC);
CREATE INDEX ix_obs_traces_skill_started ON obs_traces (skill_name, started_at DESC);

CREATE TABLE obs_spans (
    id              uuid PRIMARY KEY,
    trace_id        varchar(32)  NOT NULL,
    span_id         varchar(16)  NOT NULL,
    parent_span_id  varchar(16)  NULL,
    seq             smallint     NOT NULL,     -- 同一 trace 内的展示顺序（并列 span 的稳定性）
    name            varchar(64)  NOT NULL,     -- llm.call / tool.call / graph.invoke ...
    kind            varchar(16)  NOT NULL,     -- run|graph|node|llm|tool|mcp|db|retrieval|approval
    started_at      timestamptz  NOT NULL,
    ended_at        timestamptz  NULL,
    duration_ms     integer      NULL,
    status          varchar(16)  NOT NULL,     -- ok|error
    error_category  varchar(64)  NULL,
    content         text         NULL,         -- full 模式下的完整输入/输出，见 §7
    attrs           jsonb        NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (trace_id, span_id)
);

CREATE INDEX ix_obs_spans_trace ON obs_spans (trace_id, seq);
CREATE INDEX ix_obs_spans_kind_started ON obs_spans (kind, started_at DESC);
```

**租户隔离**：两张表都继承现有 `TenantScoped`（`tenant_id varchar(128)`），
沿用同一套 RLS 策略；所有写入与查询都在 `tenant_scope()` 内。
写入发生在后台任务里，需要显式携带 tenant（见 §6）。

**span.attrs 约定（按 kind）**。`attrs` 只放**可用于过滤与聚合的短标量**；
完整正文放在 `obs_spans.content`（`full` 模式），格式为 `{"input": "...", "output": "..."}`
的 JSON 文本——塞进 jsonb 会让「按 intent 查列表」这类查询拖上大字段。

| kind | attrs 键 | content |
|---|---|---|
| `llm` | `model`, `prompt_tokens`, `completion_tokens`, `finish_reason`, `attempt`（第几次，重试用）, `prompt_sha256`, `prompt_chars` | 完整 prompt 与完整回答 |
| `tool` | `tool`, `risk_level`, `source`(builtin/mcp/custom), `ok`, `result_chars`, `rows`（若有）, `args_sha256` | 完整参数与完整返回 |
| `mcp` | `server_url_host`（只留 host，丢 path/token）, `remote_tool`, `http_status` | 请求体与响应体（headers 打码） |
| `retrieval` | `hits`, `top_score`, `index` | 查询串与命中片段 |
| `approval` | `approval_id`, `tool`, `outcome`(pending/confirmed/cancelled) | 预览内容 |
| `db` | `table`（只记表名，不记 SQL） | — |

即使 `full` 模式，写 `content` 之前也要过一次 §7 的键名黑名单：
`{"api_key": "sk-xxx"}` 这类键值对在写入前就打码。

**保留**：`OBS_RETENTION_DAYS`（默认 14），每日低峰清理，`DELETE FROM obs_traces
WHERE started_at < now() - interval ...`，spans 按 trace_id 级联删（同一条 SQL 子查询）。
错误 trace 可选延长（`OBS_KEEP_ERROR_DAYS`，默认 30）——排查价值最高的是失败的那批。

## 5. 埋点位置矩阵

改一处不动别处；每个点都必须能在失败时**静默降级**（观测挂了不影响对话）。

| # | 位置 | span | 说明 |
|---|---|---|---|
| 1 | `chat_service._handle_message` 入口 | `run`（root） | 开始 trace；`finally` 收尾并写汇总 |
| 2 | 同上，写 user/assistant 消息前后 | `persist.message` | 只测落库耗时，不记内容 |
| 3 | 同上，`graph.ainvoke` 外层 | `graph.invoke` | 图整体；超时/异常在这里归类 |
| 4 | `agents/nodes.py::traced`，在 `graph.py` 装配处套到每个节点 | `node.<name>` | 与 `_tenant_bound` 分开：租户上下文只有读库的节点要，span 每个节点都要 |
| 5 | `planner.create_action_plan` / `respond` 里的模型调用 | `llm.call` | 读 `AIMessage.usage_metadata`；重试记 `attempt=2` |
| 6 | `tools/gateway.py::_traced_invoke`（两个执行入口共用） | `tool.call` | 入参与返回记原文（打码 `[redacted]`），键名另存 attrs 供过滤 |
| 7 | `tools/mcp_tools.py` 的远端调用 | `mcp.request` | host 级端点，不记 token |
| 8 | `knowledge/retriever.py` 检索 | `retrieval.search` | 命中数、最高分 |
| 9 | `nodes.create_and_wait_approval` / `chat_service.resume_approval` | `approval.wait` | 恢复是**新 trace**，用 `approval_id` + `parent_trace_id` 关联原 trace |
| 10 | `vision_service` 的 OCR 模型调用 | `llm.call`(kind=llm, op=vision) | 与文本模型分开统计 |

**明确不埋**：SQLAlchemy 每个查询（噪音大）、checkpointer 内部读写、jieba 分词。

## 6. 上下文传播与写入

**传播**：`contextvars`。
`trace_id` / 当前 `span_id` / 一个 span 栈放在 `app/observability/context.py`。
`async with span(...)` 负责压栈、计时、异常捕获、出栈。LangGraph 节点全是 async 函数，
contextvar 天然随 task 传播；`traced` 的 wrapper 内部开 span，保证顺序正确。
若将来引入 `asyncio.to_thread` 或线程池，用 `contextvars.copy_context()` 显式带上。

**写入**：不占用请求路径。

- `asyncio.Queue(maxsize=2000)`；`span()` 结束时 `put_nowait`，队列满则丢弃最旧并累加
  `obs_dropped_total`（宁可丢观测，不可拖慢对话）。
- 单个消费者任务：每 200 条或 1 秒 flush 一次，批量 `INSERT`（`session.add_all`）。
- `lifespan` 关停时 drain 队列（最多等 3 秒），避免重启丢最后一批。
- 写库失败只记一条 `event=obs_write_failed` 日志，**不重试、不抛**。
- 后台任务需要自己 `bind_tenant(tenant_id)`：它不在请求上下文里。

### 6.1 开关：由管理员在运行配置里决定

观测的开关**不在 `.env` 里**，而在 `platform_settings` 表的 `observability.enabled`：
控制台「运行配置 → 观测」一个开关，`platform_admin` 改，**立刻生效、不重启进程**。
`.env` 的 `OBS_ENABLED` 退化成**首次默认值**——库里没有这条记录时才读它。

为什么这么改：PAP 目前处在开发调试期，观测是排障的主要依据，所以默认开着；
而「什么时候该关」是运营判断，不该由部署脚本替管理员决定。放进 `.env` 还意味着
每次改都要登服务器 + 重启，关掉之后忘了开回来也没人知道。

**默认 true。** 要把某个环境强压成关闭（例如预发做压测），仍在 `.env` 写
`OBS_ENABLED=false`；一旦有人在控制台设过一次，库里就有记录，此后以库为准。
页面会显示当前值来自 `runtime`（管理员设的）还是 `env`（默认值）。

| 设置 | 位置 | 默认 | 说明 |
|---|---|---|---|
| 开关 | 控制台「运行配置 → 观测」 | **开** | 关掉 = 空转，见下表 |
| `OBS_SAMPLE_RATE` | `.env` | 1.0 | 只对成功 trace 生效，错误 trace 强制保留 |
| `OBS_CAPTURE_CONTENT` | `.env` | full | full / summary / off（见 §7） |
| `OBS_RETENTION_DAYS` | `.env` | 14 | 错误 trace 用 `OBS_KEEP_ERROR_DAYS`（30） |

采样、内容级别、保留期仍留在 `.env`：它们不是「要不要开」，而是「开了记多少」，
改一次需要重启是合理的，也不该让页面随手改。

```dotenv
# 首次默认值。库里有记录后以库为准，改这里不再影响已配过的环境。
# 关掉时 span() 空转、写入器不启动、OTLP 不导出，业务侧零额外开销。
OBS_ENABLED=true

# 采样率，只对「成功」的 trace 生效；错误 trace 一律保留（排查价值最高）。
OBS_SAMPLE_RATE=1.0

# 记录内容级别（见 §7）。已确认：开发期用 full，存完整原文。
OBS_CAPTURE_CONTENT=full    # full | summary | off

# 保留期：已确认 14 天。错误 trace 保留更久，便于事后复现。
OBS_RETENTION_DAYS=14
OBS_KEEP_ERROR_DAYS=30

# OTLP 出口（已确认保留）。仅在观测开关打开时导出；
# 关掉观测不会留下一个「仍在往外发 span」的隐性通道。
OTEL_EXPORTER_OTLP_ENDPOINT=
```

**关掉之后的行为契约**：

| 关掉之后 | 行为 |
|---|---|
| `span()` / `start_trace()` | 空转，只返回一个带当前时间的哑对象，不分配 ID、不写队列 |
| 写入器 | 不在跑：启动时不开任务；运行中关闭会把队列排空后停掉消费者 |
| `obs_traces` / `obs_spans` | 零新增行（表留着，**不删数据**） |
| OTLP | 不导出（`tracing.py` 只在开关打开时才初始化 exporter） |
| `/api/v1/observability/*` | 不 404，返回 `enabled=false` + 空列表 / 全 0 |
| 日志 | 一行 `event=observability_disabled`，运行时切换也会打 |

关掉只影响**新数据**：已经落库的 trace 照常可查，按保留期清理。所以「怀疑观测
拖慢对话」时可以立刻关，不需要担心丢历史。

**打开时的防呆**：`app_env=production` 且观测开启时启动打 `WARNING`
（`event=observability_enabled_in_production`），因为该模式会把完整 prompt 与回答落库。
这不是硬拦截——本项目的服务器就是开发调试用的，管理员开了就是他要开——
但每次启动都要让人看见代价与去处（控制台关，不改 `.env`、不重启）。

**为什么不做成「编译时移除」**：本项目是单体 Python 服务，拆构建产物得不偿失。
用运行时空转 + 单点判空，代价是每个埋点一次布尔判断（纳秒级），换来一份可随时开关的代码。

### 6.2 采样与丢弃

采样只决定「这条成功 trace 落不落库」；**错误 trace 强制保留**（判定在 trace 收尾时，
若已采样丢弃但状态是 error，仍然写入）。

队列满时丢最旧并累加 `obs_dropped_total`；界面显示「本窗口丢弃 N 条」，不假装全量。

**性能预算**：单 trace ≈ 10 个 span、约 2 KB；一轮对话新增写入 < 3 KB、
额外耗时 p95 < 15 ms（异步队列，不阻塞）。M2 验收时实测。

## 7. 记录内容与脱敏

已拍板：**记录完整原文**（`OBS_CAPTURE_CONTENT=full`）。理由是这套东西只在开发期开，
截断原文等于把最需要的排障信息扔掉——「模型到底看到了什么」正是要回答的问题。

但「完整原文」不等于「连密钥一起存」：

| 内容 | full 模式下的处理 |
|---|---|
| 用户消息、模型完整 prompt、模型完整回答、工具返回原文 | **原样记录，不截断**（`TEXT` 列） |
| 键名命中的敏感字段 | 值替换为 `[redacted]`，**长度与键名保留**（黑名单见下） |
| 请求头 / 端点 | 只记 host，不记 path、query、header（token 常在 query 里） |
| 附件 | 只记 `attachment_id`、`mime`、`size`、`kind`；**绝不记 base64 与文件路径** |
| OCR 原文 | 记录（本来就要给用户看，且已随消息落库） |

键名黑名单（大小写不敏感，按 `_`/`-` 归一后匹配）：
`api_key` `apikey` `token` `secret` `password` `passwd` `authorization`
`cookie` `set-cookie` `session` `credential` `private_key` `access_key`。

不截断正文、只打码密钥，是因为这两件事的危害不对等：**prompt 泄露是「开发者看到了自己
系统里的数据」；密钥泄露是「系统被别人接管」**。前者是这套工具存在的目的，后者与调试无关。

三种级别的差别：

- `full`（默认）：完整正文 + 上面那张表的打码规则。
- `summary`：正文只留前 200 字 + 长度 + sha256，与原始尺寸对比即可判断是否有大 payload。
- `off`：只记结构（span 名、耗时、状态、token 数），不记任何正文。

`full` 的代价必须写在脸上：库里会有完整的用户提问与模型回答，**保留期内等于第二份消息库**。
因此它依赖三件事成立：观测只在开发期开（§6.1）、租户隔离与消息表同级（RLS）、
14 天后自动清理（§4）。

**谁能看**：新权限点 `observability:read`，默认只授 `platform_admin` 与租户 `admin`。
普通用户只能看自己 trace 的「耗时与状态」，看不到正文——即使是自己的原文，
也没必要在诊断页里再暴露一遍。

## 8. 查询 API

全部挂在 `/api/v1/observability`，管理员权限，强制租户作用域。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/traces` | 列表。过滤：`status` `intent` `tool_name` `skill` `agent` `user_id` `conversation_id` `hours`(≤336) `from` `to` `q`（扫原文，无索引，只在窗口内匹配）`limit`(≤200) `offset`。排序固定 `started_at DESC` |
| GET | `/traces/{trace_id}` | 详情：trace + 按 `seq` 排好的 spans（前端自己建树）+ 技能命中 + 原文 |
| GET | `/overview` | 窗口卡片：轮次、错误率、**等人工轮次 `awaiting_approval`**、平均/p95/最大耗时、模型与工具调用数、token，以及 `by_status` / `by_intent` / `by_skill` / `by_agent` / `top_tools` 分布；另有 **`buckets[]` 时间分格**（每格 `traces` / `errors` / `awaiting` / `p95_duration_ms`，见下）与 `bucket_hours` |
| GET | `/agents` | 按智能体的运算基准：轮次、错误率、等人工轮次、p95、token、成本、**被拒次数**、审批与驳回、**被委派次数 `delegated_turns`**，附 `unattributed_traces`；`compare` 打开时再带上一个等长上一窗口的对比与分格走势（见下） |
| GET | `/topology` | 智能体之间**实际发生**的委派关系：`nodes[]`（配置 / 观测 / 可写 / 第几跳 / 被转交次数 `attempts`）与 `edges[]`（成功 / 暂停 / 被拒 / 失败四类，各带次数、最近时间、被拒原因、成功边的 P95）。边来自审计 `agent_delegated`，**不是** span——四种结局在 span 上分不开，见 `RUNTIME_VIEW.md` §6.1 |
| ~~`/series`~~ | — | 规划中。概览的走势已由 `/overview` 的 `buckets` 覆盖，不再单开接口；真需要「任选指标、自定义窗口」时再做 |
| ~~`/breakdown`~~ | — | 规划中。分组需求先用 `/overview` 的 `by_*` 与 `/agents` 覆盖 |
| ~~`DELETE /traces/{id}`~~ | — | 规划中。误采与脱敏目前靠保留期清理，不做逐条删除 |

`/overview` 与 `/agents` 返回 `scope`：`tenant` 表示当前身份带 `observability:read`，
`self` 表示只看得到自己发起的对话——**审计侧的「被拒」与审批数同样按这个范围收**，
否则没有该权限的成员能从拒绝计数里看出别人在做什么。

`/agents` 的四条口径（详见 `AGENTS.md` §5.3）：名字取写入时的快照、不回溯改写；
`{agents 之和} + unattributed_traces == total_traces` 恒成立；成本没报是 `null` 不是 `0`；
`delegated_turns` 与 `traces` 分母不同，不能相加也不做合计行。

最后一条的取数在 `obs_spans`（`kind=agent`），不在 `obs_traces`：委派的子调用跑在
发起方那条 trace 上，所以「自己跑了几轮」与「被叫去几次」本就分属两列。这条也决定了
名字并集要把它并进来——一个从没被直接对话选中、只被叫去干活的 Agent，原先在表里
根本不出现，而它恰恰是「谁在被使唤」要回答的那个人。

### 8.1 错误率：什么叫「错」

`/overview` 与 `/agents` 的 `errors` / `error_rate` 只数**系统没做到**的轮次。
判据写在一处（`observability.py` 的 `CONCLUSIONLESS_STATUSES`），四个取值分别对待：

| 状态 | 算不算错误 | 为什么 |
|---|---|---|
| `running` | 不算 | 还没结论。算进去的话，每次有人点发送错误率就跳一下 |
| `awaiting_approval` | 不算 | **设计好的暂停**：单子挂在审批页上，确认之后由另一条 trace 接着执行（同一 `operation_id`） |
| `abandoned` | 算 | 跑到一半进程没了，结果不可知，是事故 |
| `cancelled` | 算 | 按下取消时我们分不清「他改主意了」还是「这一轮卡到他放弃」，后者是真缺陷 |
| 不认识的状态 | 算 | **未知不等于健康**：新的失败类型应该让数字往上跳，而不是被静默忽略 |

分母仍然是窗口内的**全部**轮次，含在跑与等人工的。所以这个数是「这些工作里有多少出了错」，
不是「跑完的那些里错了多少」——后者要看 `display_state` 的分布，那是另一张表的事。

摘出去的那批必须另有去处，否则界面上像是凭空少了几十轮：

- `/overview` 的 `awaiting_approval` 字段（卡片上是一行脚注「窗口内 N 轮曾停在等人工」）；
- `/agents` 每行的 `awaiting_approval`（错误率列的 tooltip）；
- 原始分布 `by_status` 从来没动过，`running` / `awaiting_approval` 一直在里面。

> **「等人工」是窗口内的历史，不是此刻的队列。** 审批处理完之后 trace 仍保留
> `awaiting_approval` ——它记录的是那一轮当时的结论，回写等于改写历史（与「代理名字
> 不回溯改写」同一条原则）。所以卡片写的是「**曾**停在等人工」；**此刻**还有谁在等，
> 看顶部那条实时横条（`/live`，那个数是查 PENDING 单子得来的）。
> 实测（2026-11-13）：一个 7 天窗口里 25 条 trace 标着 `awaiting_approval`，
> 而 `approval_requests` 里 PENDING 为 0，对应的单子早已 VERIFIED / REJECTED / EXPIRED。
> 把脚注写成「N 轮在等人工」会让人以为系统卡住了二十多轮——那是文案撒谎，不是数据错。

> **口径分界日：2026-11-12。** 此前的 `errors` 是 `status != "ok"`（仅排除 `running`），
> 于是每一次正常的写操作都被记成一笔失败——实测一个 46 轮的窗口里有 20 条
> `awaiting_approval`，错误率长期虚高到三成以上，那个数字不再指向任何要修的东西。
>
> 因此**新旧截图上的错误率不能直接对比**。要按老口径复算，用同一次响应里的
> `by_status` 把 `error / timeout / abandoned / cancelled / awaiting_approval` 的计数
> 加起来除以 `traces` 即可，原始数据一行都没丢。
> 改动动机与搁置记录见 `TODO.md`（原 §3.1，已结项）。

### 趋势：与上一个等长窗口比

截面只能回答「现在多大」，回答不了「在不在长」。所以 `/agents` 默认再带两样东西：

- 每个 Agent 的 `buckets`：把当前窗口切成等宽的时间格（`traces` / `errors` / `denials`）；
- 一个**等长的上一窗口**（`window` 与 `previous` 两段区间都返回），给 `previous_traces`
  与 `change`。

四件事决定了这段数据能不能被信：

1. **滚动窗口，不是自然周。** 两段都止于此刻、等长。按自然周切，会让「周一上午」
   永远显得暴跌——那是窗口口径的问题，不是用量的问题。
2. **从窗口末尾往前切格。** 最后一格正好收在「现在」，已经走完；只有最旧一格可能
   被窗口起点截断（`partial=true`）。按整点对齐会让最新一格是半格。
3. **算不出比例就不算。** `change=null` 一定带 `change_reason`：
   `no_baseline`（上一窗口 0 轮，除不出比例）、`created_after_baseline_start`（Agent 在上一窗口
   开始之后才创建，那一段覆盖不完整）、`low_sample`（任一侧不足 5 轮）。
   「没法比」和「没变化」是两件事——把前者画成 0% 正是这类报表最容易骗人的地方。
4. **只比轮次。** token 与成本有缺报，拿有缺口的量算比例会凭空多出涨幅。

`compare=false` 时 `previous=null`、`change=null`、`change_reason=null`，三者同时为空
表示「这次没算对比」，与 `previous_traces=0` + `no_baseline` 区分开。

分桶在 Python 里做而不是 `date_trunc`：窗口上限 14 天、行数是千级（与 `/overview`
同一量级），而 `date_trunc` 在 SQLite 测试环境不存在。真到几十万行再换，那时
必然要配按方言分支的写法。

#### 概览的走势（`/overview` 的 `buckets`）

同一个窗口再切一遍格子，但切的这一份是给概览卡片用的：

- **切法复用 `/agents` 的三个函数**（`_pick_bucket_hours` / `_bucket_edges` /
  `_bucket_index`），不另写一套。同一页上两张走势图必须能并排读，各切一套格子，
  格与格就对不上了。
- **每格自带 `p95_duration_ms`**。窗口级的那个分位数回答不了「是刚才变慢了，
  还是一直这么慢」，而后者才决定要不要现在动手。
- **每格单独数 `awaiting`**，不并进 `errors`，也不并进「正常」：它是设计好的暂停，
  混进任一侧都会让那一层颜色说谎。
- **格子之和必须等于总量**（`tests/integration/test_observability_api.py::test_overview_buckets_cover_the_window`）。
  加起来不平的柱状图，读的人第一反应是数据错了，而那种怀疑会蔓延到同一页的所有数字。
- `bucket_hours` 显式返回，不让前端从相邻两个 `t` 相减：只有一格时减不出来，
  而「只有一格」正是 1 小时窗的常态。

分页返回 `{items, total, limit, offset}`，与现有 admin 接口风格一致。

聚合查询落在 `obs_traces` 上（已有汇总字段，不需要每次扫 spans）。数据量按 14 天 ×
每日几百轮估算是万级，不需要预聚合表；真到百万级再加 `obs_daily_rollup`（M5 预留，
不提前做）。

## 9. 控制台「观测」页

侧边栏 `observe` 从「规划中」转正，路由 `#/observe`。五块：

1. **概览卡**：8 张数字卡（轮次 / 错误率 / 平均耗时 / p95 / 模型调用 / 工具调用 /
   输入、输出 token）+ 一张**窗口走势**：堆叠柱（正常 / 等人工 / 错误）叠一条耗时
   p95 虚线（右轴）。柱子按 `/overview` 的 `buckets` 逐格画，被窗口起点截断的最旧
   一格降透明度。
   - 为什么是堆叠柱：三层加起来正好是这一格的总轮次，柱高本身就是总量，不必再画一条
     「总量」线去和三层对照——那种图最难读的正是这两者对不对齐。
   - 为什么耗时另起右轴：毫秒和轮次没有共同量纲，共轴就得二选一，而被舍弃的那个方向
     会被人按图上的位置直接读成另一个量。折线用虚线，它是方向不是读数。
   - 为什么不用 viewBox 拉伸：660 宽的坐标系被拉到 1200 时，10.5px 的轴标签会一起
     放大到 19px。控制台按容器真实像素宽度画（`ResizeObserver` 监听，收起侧栏也会
     重画——那时 window 尺寸没变，等 `resize` 事件等不到）。
   > 时间窗共用列表上方那个选择器；`/series` 仍未做，走势读的是 `/overview` 的 `buckets`。
2. **窗口构成**：两列横条——按意图（分母是轮次）与热点工具前 8（分母是调用次数，
   所以**不显示百分比**：前八名之和不是全部调用）。工具条在出错时变红，悬停写出错次数。
   不用饼图：分类常六七个且尾巴长，饼切不出可读角度，名字也只能塞进图例里对色。
3. **按智能体**（`/agents`）：一行一个 Agent——轮次与占比、错误率、p95、工具调用、
   token、**被拒次数**、审批与驳回、最近使用，状态列区分 启用 / 草稿 / 停用 / 已删除。
   另有「走势」列（每格一段时间的柱状图，出错格标红、被截断的最旧格降透明度）与
   「较上一窗口」（`↑ 100%` / `↓ 50%` / 持平 / 不可比）。不可比时鼠标悬停写原因，
   不填 0。点一行即把下面的列表筛成「只看它」。
   这一张表是「要不要加配额、要不要拆分工、边界是不是配窄了」的唯一数据来源，
   所以治理信号（被拒、驳回）与用量放在同一行，不拆两页。
4. **trace 列表**：表格，列为 开始时间 / 状态 / 意图 / 智能体 / 技能 / 主工具 / 耗时 / span / token。
   过滤器一行放下：时间窗、状态、智能体、技能、关键词。
5. **trace 详情**：摘要 → 路径与耗时（并排） → 阶段卡，不做时间轴瀑布。
   并排那两张回答「这一轮怎么走的」——左边走了哪条路、右边时间花在哪，排障时总是一起看；
   下面的阶段卡回答「模型看到了什么、循环了几趟、最后说了什么」。窄屏（≤1080px）自动堆成一列。
   - 「编排流程」（左）：按开始时间把 span 树摊平成盒子序列，缩进表示归属，
     连线表示调用方向；同名节点不合并（`规划 → 执行工具 → 规划` 就是回路走了两趟），
     重复出现的节点标 ①②。循环里的行与这里的盒子指向同一 span，选中态同步。
   - 「链路耗时」（右）：只聚合 node 级 span（`llm` / `tool` 是节点的子级，一起加会算两遍），
     条从 0 起、宽度是占总耗时的比例，读的是「谁占得多」。
   - 「Skill 选择」：命中方式（`explicit` / 触发词）、注入正文长度与上限、白名单把工具
     从几个收窄到几个、近 7 天命中次数与最近一次时间、这一轮是否真的写进了 `skill_hits`。
     数据分三处取：注入情况来自 span attrs（当时的事实），命中次数来自 `skill_hits`
     （不受观测开关与保留期影响），`exists` / `enabled` 来自 `skills` 当前状态——
     技能删了以后这一节仍能打开，只是标注「已删除」并说明统计已随级联清空。
   - 「准备工具」：模型当时实际看到的工具清单（`llm.call` 的 `tools.names`，默认收起）。
     记的是名字清单而不是「当前注册表」，否则会显示成「当时看过」。
   - 「模型与工具循环」：按趟列 `规划 → 它调用的工具`，每趟带模型耗时与 token、
     每个工具带成功/失败与耗时；顶部给出调用次数与两段合计耗时。
   - 「最终答复」：回答正文按**富文本**渲染（与聊天气泡同一套渲染层：Markdown、表格、
     曲线图、```svg 流程图都能显示），下面挂一个默认收起的「回答原文（Markdown）」折叠——
     富文本会吞掉记号，要抄命令、要核对模型到底写了什么时得能拿回原样。
     另外带输出 token 与定型耗时（`respond` span）。

实现约束（与现有前端一致）：无框架，`console.js` 里新增 `mountObserve` / `mountObsDetail`；
阶段卡片与流程图都是 DOM + CSS（不引 D3，也不用手写坐标换算）；
轮询用 `setTimeout` 串行（不用 `setInterval`，避免慢请求堆叠）。

## 10. 与现有模块的分工

| 数据 | 归属 | 关系 |
|---|---|---|
| 谁在什么时候问了什么、结果如何 | `agent_runs` + `messages` | 业务事实，不因观测退役而变 |
| 这一轮内部怎么走的、慢在哪 | `obs_traces` + `obs_spans` | 诊断明细，可清理，可丢弃 |
| 合规审计（登录、权限变更、审批动作） | `audit_logs` | 不合并：保留期与访问控制要求不同 |
| 主机/进程/磁盘健康 | `watchdog` + `/ready` | 不重复实现 |
| 外部 APM 对接（若将来需要） | `tracing.py` + OTLP | M5 把 span 桥接过去，二选一不冲突 |

## 11. 退役 LangSmith 与上线前关闭观测

### 11.1 退役 LangSmith

1. `.env`：删除或置 `false`（**注意**：`LANGCHAIN_TRACING_V2` 优先级高于
   `LANGSMITH_TRACING`，只改后者无效，见 `docs/STUDIO.md:178`）。
2. `.env.example`：删掉 LangSmith 那一整段（原 165-174 行），换成「观测已自研，
   见 docs/OBSERVABILITY.md」加一段 `OBS_*` 说明。
3. `docs/STUDIO.md` 第 6 节：改为「Studio 只用于图形调试；调用链看控制台观测页」，
   删掉 LangSmith 排障表。
4. 服务器 `/personal-agent-platform/.env` 同步删除（`deploy/sync.sh` 不动 .env，需手工）。
5. 依赖：**不卸载** `langsmith` 包（langchain-core 的传递依赖），只保证不启用。
6. 验收：跑一轮对话后，进程日志无 `LangSmith` / `tracing` 相关输出；
   LangSmith 控制台无新 run（可由 API key 用量确认）。

### 11.2 开关状态怎么确认

> 这一节原来是「上线前关闭观测」的发版清单。现在开关归管理员（运行配置 → 观测），
> 默认开启，**不再把「关闭」当成发版前置条件**——本项目的服务器本身就是开发调试用的。
> 保留这一节是为了两件事：确认开关真的生效了，以及真要清历史数据时不搞错顺序。

| # | 检查什么 | 怎么做 |
|---|---|---|
| 1 | 当前是开还是关、值从哪来 | 控制台「运行配置 → 观测」：开关状态 + 下面一行写明来自 `runtime`（管理员设的）还是 `env`（`.env` 默认） |
| 2 | 开启时启动日志 | `grep -a observability /var/log/pap-api.log \| tail -3` → 有 `observability_enabled`；`app_env=production` 时另有一条 `observability_enabled_in_production`（提醒原文落库，不是失败） |
| 3 | 关闭时启动日志 | 同一条命令 → `observability_disabled`。**运行时切换也会打这一行**，所以「关了」与「进程没重启」能分开 |
| 4 | 接口与开关一致 | `GET /api/v1/observability/overview` 的 `enabled` 字段应与控制台显示一致；关闭时是 `false` + 全 0，不是 404 |
| 5 | 关闭后确实不写 | 关掉后跑一轮真实对话，`SELECT count(*) FROM obs_traces;` 不变 |

日志位置要记牢：unit 把 stdout/stderr 重定向到了 `/var/log/pap-api.log`，
`journalctl -u personal-agent-platform` 里只有 systemd 自己的几行。

**确实要清历史数据时**（例如把开发库交给别人、或导出后想腾地方）：

```sql
-- 顺序不能反：先确认开关已关，否则清完立刻又被写回。
DELETE FROM obs_spans;
DELETE FROM obs_traces;
```

表**保留不 drop**，下次开开关直接用。`full` 模式下 `obs_traces.input_text` /
`obs_spans.content` 存的是用户完整提问与模型完整回答，**等于第二份消息库**；
要留档就 `pg_dump -t obs_traces -t obs_spans` 导出成离线文件再删。

## 12. 分阶段

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **M0** | 退役 Smith；本文定稿；`OBS_*` 配置入 `config.py`；`.env.example` 收敛成一段 | env / 文档 / 配置 | 0.5 天 |
| **M1** | 建表迁移（`obs_traces` / `obs_spans` + RLS）；`context.py` + `span()` + 队列写入；10 个埋点中的 1/3/4/5/6 先行 | 库里能看到完整 trace 树 | 1 天 |
| **M2** | 查询 API（traces/detail/overview/series/breakdown）+ 权限点；单测 + 集成测试 | 接口可用，`curl` 能出整条链路 | 1 天 |
| **M3** | 控制台观测页（概览 + 列表 + 分阶段详情 + 原文）；会话页加「看这一轮的 trace」入口 | 可用的界面 | 1.5 天 |
| **M4** | token 与成本（模型配置加可选单价）；错误分组；慢请求与错误率接入 watchdog 告警 | 告警 + 成本 | 0.5 天 |
| **M5** | 保留清理定时任务；可选 OTLP 桥接；`obs_daily_rollup`（按需） | 运维闭环 | 0.5 天 |

M1 之后每一步都能独立上线，不阻塞发版。

## 13. 验收标准

可执行、可回归（关键几条会写成测试）：

1. 一轮真实对话后 3 秒内，`/observability/traces` 能查到该 trace，
   span 数 ≥ 6，且 `run.duration_ms` 与 `agent_runs.finished_at - started_at` 误差 < 50 ms。
2. 工具失败的一轮，trace 的 `error_category` 与 `ChatResponse.error_category` 一致。
3. **观测故障不影响业务**：把 `obs_spans` 改名后跑对话，HTTP 仍 200，日志出现一次
   `obs_write_failed`，队列不无限增长（丢弃计数增长）。
4. 并发 20 轮，对话 p95 相比「控制台关掉观测」增幅 < 10%。
5. 租户隔离：A 租户的 token 查 `/traces/{B 的 trace_id}` 返回 404（不是 403，避免探测）。
6. 脱敏：构造一个 `api_key` 出现在工具参数里的用例，库里 `attrs` 不见明文。
7. 保留：把 `started_at` 改到 15 天前，清理脚本执行后该 trace 与 spans 同时消失。
8. 控制台关掉观测后零写入、`span()` 空转；压测耗时与未接观测时无差异。
   **不需要重启**——同一个进程里切换完，下一轮就不写。
9. **生产提示**：`app_env=production` + 观测开启时启动，日志出现
   `event=observability_enabled_in_production`（提示原文落库，不是失败）；
   关掉后只出现 `event=observability_disabled`。

## 14. 风险与取舍

| 风险 | 应对 |
|---|---|
| 表膨胀 | 只存摘要与结构化 attrs；14 天保留；必要时降到只存错误 trace |
| 埋点侵入主链 | 统一用 `span()` 上下文管理器 + 异常吞掉；不在业务里写 try/except |
| 观测数据本身泄露敏感信息 | 默认 summary 模式；参数只存键名；端点只存 host；黑名单 + 白名单双重 |
| 与 `agent_runs` 语义重叠 | 明确分工（业务事实 vs 诊断明细），`run_id` 关联，不合并表 |
| 队列满时丢数据 | 丢弃最旧 + 计数；界面显示「采样/丢弃」提示，不假装全量 |
| 时区与时钟 | 一律 `timestamptz`、UTC 写入，前端按本地时区显示（与气泡时间一致） |
| 成本计价不准 | 单价可由管理员配置，未配置就只显示 token，不估算金额 |

## 15. 已拍板的决定（2026-11-05）

| 问题 | 决定 | 落地影响 |
|---|---|---|
| 保留期 | **14 天**（错误 trace 30 天） | 不做 `obs_daily_rollup`，不做月度用量分析；清理任务按 trace 级联删 |
| 是否记录原文 | **记录完整原文** | `OBS_CAPTURE_CONTENT=full` 为默认；表用 `TEXT` 列；清数据的操作见 §11.2；密钥仍打码（§7） |
| OTLP 出口 | **保留** | M5 做 span → OTLP 桥接；导出受观测开关控制，关掉即不外发 |
| 运行定位 | **开发调试期默认开，由管理员决定开关** | 存在 `platform_settings`，控制台「运行配置 → 观测」；关闭时零开销零外发（§6.1）；生产开启时启动提示一次 |

14 天 + 完整原文的组合是自洽的：数据量大、敏感度高，靠**短保留期 + 开发期才开**
控制影响面，而不是靠截断内容牺牲可用性。

---

实施从 M0 + M1 开始；每个阶段结束按 §13 的对应条目验收后再进下一阶段。

## 16. 实施记录

### M0（已完成）

- `.env.example` / `.env` / 服务器 `.env`：删除全部 `LANGSMITH_*` 与
  `LANGCHAIN_TRACING_V2`，换成 `OBS_*`。`docs/STUDIO.md` 第 6 节改写为
  「Studio 只看图结构，调用链看控制台观测页」。
- `config.py`：`obs_enabled` 默认 **false**（漏配即关闭，不往库里灌原文）、
  `obs_capture_content`、`obs_sample_rate`、`obs_retention_days=14`、
  `obs_keep_error_days=30`、`obs_queue_size`、`obs_max_content_chars`。
- `langsmith` 包不卸载（langchain-core 的传递依赖），只保证不启用。

### M1（已完成）

落在库里的是一条可还原的树：`persist.user_message → graph.invoke →
begin_turn → plan_request → llm.call → … → respond → persist_state →
persist.assistant_message`。

三处实现细节与计划不同，都是被测试逼出来的：

1. **`traced` 与 `_tenant_bound` 分开**（原来是「在 `_tenant_bound` 里开 span」）。
   端到端测试发现瀑布图里没有 `begin_turn` 和 `respond`——这两个节点不碰数据库，
   本来就没套 `_tenant_bound`。而 `respond` 恰恰是答复成型的节点。
   现在 `graph.py` 装配时统一套 `traced`，租户上下文各管各的。
2. **`seq` 必须单独计数**。span 是「退出时才 append 进列表」的，用
   `len(spans)` 当序号会让同时打开的同级 span 全拿到 0，详情里的先后顺序全乱。
3. **`conversation_id` 要在 `finish()` 里转 `uuid.UUID`**。业务侧一直是字符串，
   列类型是 `Uuid`；不转的话写库报 `'str' object has no attribute 'hex'`，
   而且是**写在后台任务里、只留一条日志**——对话照常返回 200，观测一行没有。

### 关闭与开箱

- 关：控制台「运行配置 → 观测」关掉（或 `.env` 的 `OBS_ENABLED=false` 作为首次默认）
  → 写入器停掉、`span()` 空转、`trace_run` yield `None`。端到端测试断言
  「同样的对话，库里一行都不多」。
- 开：同一个开关打开即可，**不重启**；`app_env=production` 时启动日志会提醒
  「完整 prompt 会落库」。

### 已知边界（M2 起再做）

- 没有查询 API 与界面（M2/M3），现在只能查库。
- 没有清理任务（M5），`OBS_RETENTION_DAYS` 目前只是配置，尚未生效。
- OTLP 桥接未做（M5），当前 span 不导出。

### M2 + M3（已完成）

查询接口与控制台页面一起上线：`/api/v1/observability` 三个接口 + 侧边栏「观测」页。

**与 §8 / §9 计划的差异**（都是实施时才看清的取舍）：

| 计划 | 实际 | 原因 |
|---|---|---|
| `page` / `page_size` | `limit` / `offset` | 跟 `conversations` 接口一致，前端只有「一页够不够」的判断 |
| `p95` 用 `percentile_cont` | Python 里算 | 测试跑 SQLite，没有这个函数；14 天量级抽一列 `duration_ms` 回来更便宜 |
| `/series` 折线 | 未做 | 概览四张卡已经能回答「是不是变慢了」；折线等 M4 跟成本一起做 |
| `/breakdown` | 并入 `/overview` | 页面只需要 status / intent / 工具三组，单独开接口没有调用方 |
| `DELETE /traces/{id}` | 未做 | 观测是只读的调试手段，误采敏感内容的处置走 M5 的清理任务 |
| 详情用现成 trace viewer | 自绘阶段卡片 + 流程图 | 要回答的是「技能/工具/循环」，不是时间轴；DOM + CSS 更省事 |
| 漏斗式的「管理员权限」 | 默认只看自己 | 见下 |

**权限模型改了**：计划里观测接口统一要管理员，实际改成按可见范围收敛——

- 任何成员都能进观测页，但**默认只看到 `user_id` 是自己的 trace**；
- `observability:read`（或平台管理员）才看得到本租户全部，并可用 `user_id` 过滤；
- 别人的 `trace_id` 拿到是 **404 不是 403**，403 等于确认这个 id 存在。

这么改的理由：观测是开发期排障工具，配置一错就 403 会让「功能坏了」和「没权限」混在一起；
而「自己问过什么」本来就不该对本人保密。

**生产环境如何放开到全租户**（现在 `dev` 角色只有 6 个旧权限，不会自动获得新的）：

```sql
INSERT INTO role_permissions (role_id, permission)
SELECT id, 'observability:read' FROM roles WHERE name = 'dev'
ON CONFLICT DO NOTHING;
```

`app/auth/dependencies.py` 与 `scripts/seed_dev_data.py` 里的 `DEV_PERMISSIONS` 已加上这个权限，
但 `_ensure_role` 只在**角色不存在时**播种，所以已有库仍需上面这一句。

**前端实现要点**：

- 列表只查汇总列，原文只在详情接口取（一轮 prompt 上万字符，列表带它就是几百 KB）；
- 流程图层级由 `parent_span_id` 现算，`seq` 只是稳定排序，不假设它递增即层级；
- 搜索框输入 350ms 防抖；`q` 走 `input_text`/`output_text` 的 `ILIKE`，无索引，
  只在时间窗内匹配，定位是「记得那句话、不记得哪一轮」的补救手段。
