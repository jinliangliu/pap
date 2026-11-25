# 事务能力规划

> 状态：边界已定，P0 全部完成、P1 三类事务链路均已打通。本文取代早期「不做长驻开发环境」的非目标表述。
> 对应能力：每人各自的办公事务——编制文档、编程、定时任务，形态类似云端电脑，但**不是**交互式桌面。

## 1. 已定决策

| 项 | 结论 | 直接后果 |
|---|---|---|
| 网络 | 沙箱一律 `--network none`，默认断网 | 沙箱只做计算与文件变换；**所有出网走平台侧已审批的连接器/工具** |
| 工作区 | 先做单机版，本地盘 | 不做共享存储抽象；多副本部署不支持，扩容前先迁存储 |
| 回滚点 | 每次任务前快照 + 时间轮转 | 快照是任务生命周期的一部分，不是可选备份 |
| 计量 | 表结构加 `cpu_ms`、`status`；执行记录（Trace/State）持久化 | 记录从沙箱进程内存搬进 PG；墙钟时长与 CPU 时间分开记 |

两条由此推出的硬约束：

- **沙箱不持有凭据、不联网。** 代码与依赖预置（§7）属于未来引入本地挂载时才接的通道，本期编程事务把代码作为数据经 stdin 送入一次性 tmpfs；发邮件这类动作由沙箱产出草稿，投递交给平台侧工具，按 §5 的授权范围校验。
- **执行记录的写入方只有沙箱服务端。** 平台只读回，不另存一份"结果副本"——同一份事实存两处，迟早有一处对不上。

## 2. 与现有沙箱的关系

| | 现在：函数调用 | 目标：事务 |
|---|---|---|
| 生命周期 | 一次执行一个一次性容器，结束即销毁 | 一个任务包含多次执行，跨分钟到小时 |
| 状态 | 无；`--read-only` + 64MB tmpfs，容器销毁即丢 | 每用户一块持久工作区 |
| 记录 | 内存，`execution_ttl_seconds=600` | PG 常驻，含状态迁移轨迹 |
| 失败语义 | 超时/不可达 `retryable=false` | 沿用：只标 `unknown`，不猜、不自动重发 |
| 网络 | `--network none` | 不变 |

协议不变：`POST /v1/execute` + `GET /v1/executions/{id}` + `GET /v1/health`，只增加任务/快照标识头、CPU 计量回传与 Trace 写入。**不挂宿主工作区**（见 §5 编程事务形态）。

## 3. 数据模型

### `workspaces`
一人一块，唯一约束 `(tenant_id, user_id)`。

| 字段 | 说明 |
|---|---|
| `tenant_id`, `user_id` | 归属；RLS 按租户过滤，读路径再校验用户 |
| `root_path` | 服务端生成的单机绝对路径，客户端不可传 |
| `status` | `active / idle / archived / deleted` |
| `quota_bytes` | 0 = 沿用平台默认 |
| `created_at`, `updated_at` | 归档与删除均写审计 |

### `tasks`（事务本体）

| 字段 | 说明 |
|---|---|
| `id`, `tenant_id`, `user_id`, `workspace_id` | 归属 |
| `kind` | `document / code / schedule` |
| `status` | `queued / running / awaiting_approval / awaiting_input / succeeded / failed / cancelled / unknown` |
| `idempotency_key`, `retry_count` | 定时任务重复触发、人工重试共用 |
| `cpu_ms` | 该任务下所有执行之和；沙箱未回报时为 `NULL`，**不是 0** |
| `cancel_requested_at`, `deadline_at` | 取消是请求，不是即时的状态改写 |

### `schedules`

`cron`、`next_fire_at`、`idempotency_window_seconds`、`task_template_json`、`last_task_id`、`enabled`。单实例 + advisory lock 防重；触发时先 resolve `(tenant_id, user_id)`，再显式构造 `contextvars.Context()`——定时触发没有请求上下文，漏了这步会带空租户写库并抛 `TenantIsolationError`。

### `task_executions` 与 `execution_trace`

- `task_executions`：`id` 即沙箱 `execution_id`，含 `task_id`、`status`、`cpu_ms`、`duration_ms`、`exit_code`、`policy_revision`、`snapshot_id`、stdout/stderr 的 `artifact object_key`。
- `execution_trace`：`execution_id, seq, from_status, to_status, occurred_at, actor, detail_json`，**追加写、不更新**。
- `execution_state`：`execution_id, state_json, updated_at`，当前状态快照。

`cpu_ms` 由沙箱侧从 cgroup `cpu.stat` 的 `usage_usec` 差值换算，平台只落库；`duration_ms` 是墙钟，两者不能混用。沙箱服务端直写同一 PG 的专用表，只授予这几张表的 insert/update 权限；平台侧 `get_execution()` 按 `tenant_id` 回读，查不到 → `EXECUTION_RECORD_MISSING`，不重试。

## 4. 快照与轮转

- **触发**：每次任务开始前，含定时任务的每一次触发。
- **存储**：单机本地 `workspaces/<id>/snapshots/<snapshot_id>/`，优先 reflink，退回普通复制。
- **保留**：最近 3 次 + 每个半小时桶内最早的一次（桶上限 48，即 24 小时）；两者取并集，总上限 51 份。
- **清理**：同一规则既决定保留也决定删除；删除写审计。
- **执行与写回**：**已决策：失败留痕**。直接写工作区，快照只用于人工回滚；任务失败时已写入的内容留在工作区，不自动回退。改成「失败不留痕」（在快照副本上执行、成功后再切回）需另立一轮，不因一次失败临时切换。

## 5. 分期与验收

### P0 打基础（用户看不见）—— 已实现

1. ✅ 执行记录持久化：沙箱服务端直写 PG（`deploy/sandbox/records.py`），配 DSN 启用、留空回落内存；终态+trace+state 同事务提交。
2. ✅ `cpu_ms` 采集：容器内 `getrusage(RUSAGE_CHILDREN)` 差值，全链路透传；缺字段落 `NULL`，不填 0。
3. ✅ `workspaces` 表 + 单机路径（服务端生成、防穿越）+ 快照轮转与清理审计（`app/services/workspace_service.py`、`snapshot_service.py`）。
4. ✅ `tasks` / `task_executions` / `execution_traces` / `execution_states` 四表与状态机（`app/services/task_service.py`）：合法迁移表、`unknown` 恢复、取消是请求、`cpu_ms` 累加保持 NULL 语义。
5. ✅ `schedules` + 单实例触发器 + 租户上下文构造（`app/services/schedule_service.py`）。**注**：`schedules` 表由迁移 `c1f7b2e94a83` 补建——上一版 `b9d4f2a71c60` 建五表时遗漏。cron 按 UTC+偏移解释；SQLite 读回的 naive datetime 一律按 UTC 补齐（否则下次触发算到过去，重复入队）。
6. ✅ 配额扩三项：每人并发任务数、CPU-秒、磁盘（`TenantQuota`）。

激活路径：维护循环 `app/workers/maintenance.py` 逐租户调用在途任务恢复，循环外用 `fire_due_schedules()` 触发定时任务（服务内部自己进 system scope 读 due 行，逐条构造独立租户 Context 写库）。

**验收**：40 分钟任务跑完，审批回读一致且 `cpu_ms` 非空；服务重启后在途任务标 `unknown`，既不消失也不假装成功；连续触发 200 次任务后快照数 ≤ 51。
**单测**：`tests/unit/test_transaction_services.py`（纯逻辑）+ `tests/integration/test_transaction_integration.py`（隔离/文件/上下文）。

### P1 三类事务各通一条

| 事务 | 链路 | 状态 |
|---|---|---|
| 编制文档 | Markdown 起草 → `python-docx` 出 docx → 进 `artifacts`；不引入 LibreOffice | ✅ `app/services/task_runner_service.py` |
| 编程 | 代码基线/改动随 stdin 进一次性沙箱 → 沙箱内写 tmpfs、出 unified diff、可选跑白名单测试 → diff 进 `artifacts`；工作区只在平台侧留档与快照 | ✅ 同上（内容随请求走形态，见下） |
| 定时任务 | `compute`（document/code）出草稿 → 平台侧工具投递；投递失败不影响计算产物 | ✅ 同上，附 `delivery_grant_service` / `delivery_service` |

**编程事务的形态（这一轮定的边界）**：沙箱仍然**不挂宿主目录、`--network none`、
只读根、非 root、一次性容器**——§7「依赖预置」里的 bare mirror / wheel 目录是
未来引入本地挂载时才接的通道，本期不走。代码基线（`base`）与改动（`files`）作为
JSON 走 stdin，平台固定模板在 `/work/code` 的 tmpfs 里写文件、用 `difflib` 出
unified diff、可选跑测试。产物（`changes.patch`，空 diff 合法＝无改动）与测试结论
分两个标记输出：测试未过**不影响**「diff 已产出」这个事实，结论如实写进
`output.test`，不把失败测试伪装成全绿。输入校验（相对路径/字符与文件数上限/
`python3 -m pytest|unittest` argv 白名单）全在进沙箱前完成，非法输入＝拒绝，不标
unknown、不进沙箱。工作区侧把 `files` 留档到 `<root>/code/<task_id>/` 供事后复现，
快照仍按 §4 在任务前打。

`clone/fetch/push` 仍由平台侧工具执行并单独审批，不在本期链路内。

**DOCUMENT 已实现的链路**：用户输入（title + Markdown）作为 JSON 走 stdin，
代码是平台固定模板（内容不进代码，避免注入）→ 沙箱内 `python-docx` 出 docx
→ base64 走 stdout `PAP_ARTIFACT:` 一行 → 平台解码落 `artifacts` →
`get_execution()` 独立回读一致后才推 SUCCEEDED。`result.truncated` 与
PK 头 + 最小长度双重检查挡住半份产物；沙箱异常落 UNKNOWN 不重发。

**执行记录只有沙箱写**：`task_executions` 的 status/cpu_ms/task_id/snapshot_id
由沙箱直写 PG；平台只写 `tasks.output_json` 的产物引用，不在同一行双写。

**已修的 P0 缺陷**：`task_executions.task_id` 原为 NOT NULL，但一次性代码执行
不带 task_id——配 DSN 后每次执行都会落库失败返回 503。迁移 `d4a7c9e13b62`
放开约束；`accumulate_cpu_ms(None, 10)` 原返回 None，把「还未累加」与
「一次缺失」混同，导致首次执行永远存不进 cpu_ms，已改为首次回报落值。

审批在这期换形态：每日任务不可能每天等人点。引入**授权范围**（capability grant）——一次授权「本任务可写我工作区、可发这 3 个收件人、有效期 90 天」，之后每张单验的是「是否落在范围内」。

**定时任务投递的形态（§8 开放项 4 已定）**：**按 schedule 授权**，不按人、不按 workspace——按人会让此人所有定时任务共享一份权限，按 workspace 会跨任务串权。授权凭证挂在 `schedules.delivery_grant_json`，由 `authorize_delivery()` 在用户请求里一次写入（只能本人授权自己的 schedule），`revoke_delivery()` 撤销；触发时快照进 `tasks.delivery_grant_json`，投递只认这份快照、不回读 schedule。凭证只含 `tool_name`、`parameter_bounds`、`expires_at`（默认 90 天）与授权人，**不含 roles/scopes**——那会把一次授权变成可反复使用的提权凭证。

**撤销即时打断投递**：`revoke_delivery()` 按 `tasks.schedule_id` 精确反查 `queued/running` 任务，级联改写它们手里的授权快照（整体重新赋值，JSON 列不跟踪原地修改），这些任务随后即使跑完计算也会被 `check_delivery` 拒绝出网。改的是投递授权，**不是任务状态**——计算不需要外部授权，撤销不抹产物；已 `succeeded` 的任务不回改，因为投递已发生、撤销收不回来。边界：已经发出的那一次网关请求无法撤回；这里挡的是「还没开始投递」的任务，不引入跨请求取消令牌（见 §7 直写 PG 的约定）。

投递链路（`delivery_service.deliver_task_output()`）：

- 模板形态 `{"compute": {"kind": "document|code", ...}, "delivery": {"tool_name": ..., "arguments": {...}}}`。compute 复用 DOCUMENT/CODE 执行器；delivery 是平台侧动作，不进沙箱 stdin。
- `check_delivery()` 是 **closed set**：参数键必须全在白名单内，多传一个 `bcc` 就拒绝；白名单值为 `*` 只表示「这个键每次可不同」，不是「没写就随便」。拒绝信息只说键名，不回显收件人/正文。
- 每次投递**重新解析用户当前 RBAC 权限**构造 `ToolExecutionContext`，`is_platform_admin=False`。撤权后下一次触发立即被网关拒绝；网关原有的 scope / 租户策略 / 站点授权照常生效，`execute_approved` 不是绕过校验的后门。
- 投递结果写 `task.output_json["delivery"]` 与独立审计，任务终态由计算决定：`denied / failed / unknown` 都不抹掉已落库的产物，也不伪装成已送达。超时/不可达标 unknown，不自动重发。
- 授权与撤销的 HTTP/UI 入口留 P2（与 schedule 本身的配置入口一起做）；当前入口是服务层函数。

**验收**：三类任务连续 7 天无人值守；断网沙箱内的编程任务能完成；每次失败都能说出原因，不是「重跑一次就好了」。

### P2 体验与计量

任务列表 UI、工作区文件视图（只读 + 下载）、按人计量、长驻容器（仅当确有"人在等"的证据，idle 15 分钟回收）。这期才谈「云端电脑」。

### 配置入口与权限（已落地）

| 配置 | 入口 | 谁能改 |
|---|---|---|
| 每人并发任务数、每日 CPU 秒、工作区上限 | 控制台「组织与成员」→ 租户「配额」；`GET/PUT /api/v1/admin/tenants/{id}/quota` | **仅平台管理员** |
| 沙箱服务端资源与容量（`PAP_SANDBOX_*`） | 沙箱服务进程的环境变量，重启生效 | 部署方 |
| 断网、只读根、零宿主挂载 | `runtime.py` 代码硬编码 | 不可通过配置改变 |

普通租户有**三层**拦截：管理导航项不渲染；深链落 `adminGate`；接口 `require_platform_admin` 返回 403。只靠前端藏字段不算权限，只靠后端会让人看见点不动的入口。

配额页面里工作区上限以 MB 输入、API 按字节存储（1MB = 1024²），避免「页面填 MB、库里当字节」造成 1048576 倍误差。0 = 沿用平台默认，且读取配额不会为了回显而建行。

## 6. 明确不做

- 共享工作区：协作交给外部 git 远端，各人 clone 各自的工作区；平台不做共享卷、双轨权限、并发合并。
- LibreOffice、中文字体、GB 级镜像；`python-docx` 足够。
- 沙箱出网、出网白名单；单机多副本与共享存储。
- GPU 沙箱、交互式 Notebook。

## 7. 技术深水区与实现约定

| 风险 | 结论与约定 |
|---|---|
| gVisor 兼容性 | 断网设计下**不存在网络 I/O 损耗**，只剩系统调用与文件 I/O。当前只有 Python/JS 纯计算 + 64MB tmpfs，C 扩展/`ioctl`/`proc` 问题要到 P1 引入依赖才会出现。P0 加基准闸：三类负载（数值计算、tmpfs 读写、git 操作）对比 runsc/runc 并定阈值；不过就缩依赖白名单，**绝不为了兼容性退回 runc**——边界退化不可逆。 |
| 直写 PG 的并发 | **已实现**：`deploy/sandbox/records.py`，异步连接池（默认上限 4），终态 + 2 条 trace + state 在同一事务里同步提交；幂等靠 `ON CONFLICT DO NOTHING/UPDATE`。终态必须同步落库后才返回，否则审批回读读到未提交数据，verifier 直接失效。先**不上 MQ**（新组件 = 新故障域）；触发条件：写入 P95 > 50ms 或连接池持续打满，再上 Redis Stream。 |
| 定时任务上下文 | 已有 `bind_tenant()` / `system_tenant_scope()` / ORM 自动过滤事件，**不需要新造封套类**。约定：从 `schedules` 行取 `(tenant_id, user_id)` → `copy_context()` → `bind_tenant()` → `create_task(..., context=ctx)`；取不到租户就拒绝启动，不进入写库。不得用 `system_tenant_scope()` 图省事，那会让所有定时任务以系统租户写库、审计全丢。 |
| 依赖预置 | P1 只支持声明式清单（`requirements.txt` / `package.json`）且变更走审批，不做任意在线 install。平台侧 `pip download --only-binary=:all:` 预拉 wheel；沙箱内 `pip install --no-index --find-links=/deps`。长尾依赖进不了白名单要明确拒绝，不静默失败。wheel 在平台侧只下载不执行，执行发生在沙箱内——这正是隔离边界的意义。git 同理：平台侧维护 bare mirror，沙箱内 `git clone /repos/<id>.git`（本地路径，不算出网）。 |
| 快照的文件系统 | reflink 只在 XFS/Btrfs 上可用，且必须同一文件系统内。ext4/WSL2 退回普通复制，GB 级工作区不可接受——因此给 workspace 设硬上限（建议 512MB，可调）。P0 启动自检里探一次 reflink 能力，不支持就显式告警，不默默变慢。 |
| `cpu_ms` 采集 | 不硬编码 cgroup 路径（v1/v2、systemd driver、云宿主差异都不同）。容器内 runner 用 `getrusage(RUSAGE_CHILDREN)` 在子进程前后取差值——只算代码真正消耗的 CPU，不含容器启动开销，且不依赖 cgroup 路径。拿不到 → `cpu_ms=NULL`，不能填 0（0 = 测到了且没消耗）。（已实现于 `deploy/sandbox/image/runner.py`。） |
| 生命周期抽象 | 平台侧「零改动」靠的是 HTTP 协议契约，不是 provider；provider 抽象解决的是**沙箱服务端**换后端时的改动量。现状：`deploy/sandbox/runtime.py` 是模块级函数（`probe` / `run_container` / `destroy_container` / `sweep_orphans`），服务端 `import runtime as backend`。抽成 Protocol：`probe / create / destroy / sweep / get_stats / snapshot`，先实现 `DockerProvider`，将来接 `K8sProvider`；平台侧只通过请求头与响应字段感知能力。 |

## 8. 开放项

1. ~~沙箱服务端直写 PG 的最小权限方案是否可接受~~ **已定：直写 PG**（`deploy/sandbox/records.py`）。代价与三条缓解措施见 `docs/SANDBOX.md`；回退路径是清空 `PAP_SANDBOX_DATABASE_URL`。
2. ~~沙箱侧能否稳定采集 CPU 时间~~ **已实现**：容器内 `getrusage(RUSAGE_CHILDREN)` 差值，不依赖 cgroup 路径；拿不到落 `NULL`。
3. ~~快照是否需要「失败不留痕」~~ **已定：失败留痕**（见 §4），快照只用于人工回滚。
4. ~~授权范围的形态~~ **已定：按 schedule**（见 §5；凭证不存 scopes/roles，每次现取当前 RBAC 权限）；~~撤销是否打断已入队任务~~ **已定：即时打断投递**（级联改写任务快照，不取消计算，见 §5）。
5. ~~单机故障即在途任务全丢~~ **已定：不允许重放**。在途任务标 `unknown` + 人工确认，不自动重发；沙箱不可达/超时同理。理由见 §2「失败语义」。

## 9. 现在就做的一件事（已完成）

执行记录已从沙箱进程内存搬进 PG，`cpu_ms` / `status` / Trace / State 全部落表。
策略是「配 DSN 才直写，留空回落内存」——后者是 `SANDBOX_ROADMAP.md` §4 要求的
回滚路径，不是静默降级（启动时明确告警）。

**P0 全部完成**：§5 六项均已实现并接进维护循环。**§8 开放项已全部收口**：
3 失败留痕、4 按 schedule 授权且撤销即时打断投递、5 单机故障不重放（见 §4/§5/§8）。
