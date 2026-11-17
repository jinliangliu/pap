# 失误、根因与判据

这份文档记录的是**平台建设过程中真实踩过的坑**，以及从每个坑里提炼出的判据。
它不是变更日志，也不是自我检讨：写法刻意统一成「现象 → 根因 → 处置 → 怎么提前发现」，
用途是让下一个接手的人在遇到同类现象时先查这里，而不是重新怀疑一遍代码。

三条阅读线索：

- 只看**怎么避免**：跳到 [§5 判据清单](#5-判据清单)。
- 遇到怪异现象：先看 [§3 验证类假象](#3-验证类假象看到的不等于发生的)，这类坑的共同点是「测试全绿但功能不对」。
- 要改配置或发版：看 [§2.3 部署与运行时](#23-部署与运行时)。

## 1. 判断类失误

这一节记录的不是代码缺陷，是**判断和操作**的问题。它们的代价往往比 bug 高，
因为 bug 有测试兜底，判断错了没人兜。

| 现象 | 根因 | 处置 |
|---|---|---|
| 两个 Bearer 令牌原文出现在会话记录里 | 为了「看清 `tenants.json` 结构」把整个字典打印出来，没有先问这行输出会留下什么 | 已轮换两侧令牌并验证旧令牌 401；此后一律只打印 sha256 前 10 位指纹，原文在服务器端读取使用。**这是本项目唯一一次造成外部影响的操作失误** |
| 按「服务器 `/srv/upstream-mcp` 是 git 仓库、跑 `<commit>`」制定上线方案 | 把记忆当成事实。实际那个目录是扁平目录、不是仓库，`<commit>` 在服务器上根本不存在 | 全盘 `find / -name '*.git'` + 逐文件 md5 比对，才定位到上游在本地 `github_repo/upstream-mcp`。差量最终是 4 个文件 |
| 文档写下「OCR 服务绑 `0.0.0.0:4499`，公网可直接打」 | 把推断写成了实测口吻。看到的「外部 IP 访问日志」其实是经 SSH 隧道进来的 | 实测 `ss -lntp` 只有回环、公网探测 000，改文档并补一句「换机器重部署仍要确认」 |
| 文档写下「`ClientAliveInterval 60` 会杀闲置连接，长任务要客户端保活」 | 同上，机制没验证就下了结论 | 实测 `ssh pap 'sleep 200'` 存活 200 秒以上，推翻原结论并改写 `docs/DEPLOYMENT.md` |
| 沙箱第一版只写 handler、不写 verifier | 把「调用成功」当成「结果成立」 | 必须 `GET /v1/executions/{id}` 回读记录才允许审批推进到 VERIFIED。规则：**外部执行的返回值不能自证** |
| 导入类工具一直没声明 verifier，审批永远停在 EXECUTED | 后台确实是真入库，于是没人觉得缺了什么——`verified=false` 挂在卡上被当成正常 | 补 `verify_ingested_document`：另起会话按 `document_id` 回读，文档在 + ready + 有切片才算核验。顺带修掉回读上下文逐字段重建导致 `conversation_id` 丢失 |
| 回读核验的上下文用手抄的字段重建 | 与「确认执行」那次同一个错，只是换了个位置 | 统一 `replace(context, …)`。逐字段抄写时新增字段不会报错，只会让某条很少走到的路径静默退化 |
| `sync.sh` 的迁移步骤连着几版静默失败 | 多步命令串在一条 ssh 里，末尾用 `\| tail -2` 截断输出，退出码被 tail 吃掉 | 先归一化再校验前缀；远端加 `set -euo pipefail`；迁移段不再走 `ssh_retry`（失败重试只会重复停服并把真报错刷掉） |
| 前端替换脚本跑出 `Invalid or unexpected token` | 用 Python 批量改 JS 时丢了一个引号 | `node --check` 是这类操作的必需步骤，不是可选项 |

## 2. 架构类缺陷

按根因归类，不按时间。**每一行的根因都能对应到 §5 里的一条判据**。

### 2.1 安全边界

| 缺陷 | 根因 | 处置 |
|---|---|---|
| `auth_mode` 配置项从未被任何代码分支读取，服务无条件信任 `X-Authenticated-Subject` | 配置项写了，但没人检查「谁在读它」。一旦经反代暴露公网，带个头就能冒充任意用户 | `app/api/dependencies.py` 在 gateway 模式下缺身份头直接 401、不再回落 `dev_subject`；`app/core/config.py` 在 `APP_ENV=production` + 非 gateway 时拒绝启动 |
| `.env` 权限 644，沙箱用户能直接读出模型与数据库密钥 | 把「沙箱只读挂载」当成足够，但可读就等于可泄露 | `chmod 600` + systemd `InaccessiblePaths=/personal-agent-platform`（只读不够） |
| 四个存量容器把 6379/3307/5672/15672/8765 发布到公网，`ufw` 管不到 | docker publish 走 NAT `DOCKER` 链 + FORWARD，**不经过 INPUT** | `deploy/firewall/docker-port-guard.sh`（落 `DOCKER-USER`，`persist` 由自带 oneshot 单元保证重启后仍在） |
| 对端 MCP 无令牌即可 `GET /mcp` 建 SSE、`DELETE /mcp` 终止他人会话 | 鉴权只做在 JSON-RPC 层，HTTP 枚举没拦 | 对端 `PRV_STRICT_HTTP_AUTH=1` 启用 HTTP 前筛；实测五项行为全部符合预期 |

### 2.2 数据一致性

| 缺陷 | 根因 | 处置 |
|---|---|---|
| `documents.id` 是全局主键，但 uuid5 只含 logical_id，第二个租户入库必定 `duplicate key` | 只在默认租户跑过，「按租户入库」其实一直不可用 | `uuid5(ns, f"{tenant_id}:{logic_id}")` |
| `audit_logs.event_type` 是 varchar(26)，新增 27 字符事件名直接 `StringDataRightTruncation` | `Enum(native_enum=False)` 的列长由最长成员名推断，加成员等于改表；SQLite 不校验长度 | 迁移 `b1e7c4a9f2d8` 放宽到 64，模型显式 `length=64` |
| `tenants.status` 模型按成员名（`ACTIVE`）读写，迁移种子写的是小写 value（`active`），`/me` 直接 `KeyError` | 本地 `create_all` 两侧都由 ORM 生成、表现一致，只有跑过 Alembic 的库才暴露 | `Enum(..., values_callable=lambda e: [m.value for m in e])`，迁移里的枚举字面量同步改小写，补回归测试 |
| `messages.created_at` 用秒级 `CURRENT_TIMESTAMP`，同一秒内 user/assistant 顺序退化成 UUID 随机序 | 排序依赖的精度不够。表现为会话详情返回 `['assistant','user']` | `add_message()` 显式写 `datetime.now(UTC)`（微秒级） |
| SQLite 的 `DateTime(timezone=True)` 读出来是 naive，前端按本地时区解释，所有时间早 8 小时 | 建表声明带时区不等于驱动会补 tzinfo | `app/schemas/common.py` 的 `as_utc()`，新加时间字段必须走它 |
| `seed_dev_data` 在非默认租户被 RLS `WITH CHECK` 拒绝 | `tenant_scope` 写在 `session.begin()` 内部，而 `app.tenant_id` 是在事务 `begin` 事件里 `set_config(..., true)` 写入的，事务开完再改无效 | 上下文必须在事务开始**前**绑定 |
| `with_loader_criteria` 用闭包捕获租户，第二个租户复用第一个租户的条件 | SQLAlchemy 语句缓存键不含闭包值 | 改 `bindparam("pap_tenant_scope")` + `statement.params(...)`；且 `bindparam` 的名字必须写字面量（lambda 分析期全局变量会被换成 `PyWrapper` 代理） |
| ORM 的 `Enum(RiskLevel)` 生成 `risklevel` 类型，迁移里若写 `String(16)` 就与 ORM 不一致 | 同一件事定义两处 | 迁移复用枚举时用 `postgresql.ENUM(..., create_type=False)` |
| 自定义工具的 `retryable` 对写操作也置 true | 只看状态码，没想「502 不代表没写进去」 | 只有 `GET` 才允许 `retryable` |
| 导出类工具改 `requires_approval=False` 却不生效 | `ToolGateway` 对 `write` 及以上无条件强制审批 | 审批必须由风险等级**单点**推导（`_WRITE_RISKS`），配置里不存 `requires_approval` |
| 删会话只删了 `conversations` + `messages`，用户以为对话没了 | 一次对话落在四处，各由不同组件管：`agent_runs` 外键 `SET NULL`、checkpoint 按 `thread_id`、产物在对象存储。删一处时没人负责另外三处。线上实测 49 个会话对应 66 个 checkpoint 线程、18 条孤儿运行记录，`checkpoint_blobs.messages` 里能直接读出已删对话的中文原文 | 删除改为销毁：事务内删 messages / agent_runs / artifacts（先取出对象键），提交后删图状态与磁盘；附件目录改 `to_thread`；`scripts/purge_orphan_traces.py` 清历史残留（默认只报告）。**「删除」必须按数据实际分布逐处核对，不能按表名推断** |
| 「发现 MCP 工具」用表单里的地址，「接入」却固定用 `.env` 的地址去校验 → 在第二个 MCP 服务上接入一律报「远端没有这个工具，可能已下线或改名」 | 同一件事的两端各读一处配置：UI 允许临时指定地址，后端写着「两处共用一份判定」，但共用的是 `.env` 而不是本次请求的地址。**错误文案还把结论写死了（「已下线或改名」），把人往错误方向带** | 接入请求带上 `server_url`/`token`，与发现同一个端点；接入项自己存端点，运行时按它调用；错误改成「`<url>` 上没有名为 X 的工具」。**跨两处的操作要让「两处」用同一个入参，而不是同一个默认值** |
| `audit_logs` 在 PostgreSQL 上一行工具审计都写不进去：`DbAuditSink` 自己开新会话、没进租户上下文，插入被 RLS 拒（`new row violates row-level security policy`），异常又被 `except` 吞成一行日志 | 审计写入是**旁路**：独立会话 + 「失败不影响业务」的容错 except。两层叠加之后，失败与成功长得一模一样——代码注释、单测（SQLite 没有 RLS）、文档都认为「每次调用都记了」 | `record()` 先 `tenant_scope(payload["tenant_id"])` 再开会话；没有租户标识时显式 warn 跳过；用例盯「会话创建时上下文是否为 payload 的租户」，不依赖数据库特性。**旁路写入必须在真实 PG 上验证过一次** |
| 审批单过期/被拒/执行完之后，`agent_runs` 那条运行永远停在 `WAITING_FOR_APPROVAL`：线上积了 5 条，`finished_at` 全为空 | **一个状态只有写入点、没有回收点**。`WAITING_FOR_APPROVAL` 在三处被写进去，而 `approval_service.py` 从不写 `RunStatus`——审批的生命周期与运行的生命周期是两套代码，没人负责把它们对上。单测里审批一旦落终态就不再往下走，所以永远看不到这个缺口 | 审批落终态时按两条关联（`agent_runs.approval_id`、`approval_requests.run_id`）收口，`UPDATE ... WHERE status = WAITING_FOR_APPROVAL` 保证幂等且不覆盖已完成的行；另加 `reconcile_waiting_runs` 兜底对账（10 分钟宽限）。**给一个状态加写入点时必须同时问「谁把它改回去」** |
| 追这条缺陷时，我把影响说成「看板挂 5 个点不动的待办、污染 `/observability/agents` 的错误率与 p95、`finished_at` 为空」——三条全错：前端按 `approval_id` 逐张取审批状态，`/agents` 读的是 `obs_traces` 而非 `agent_runs`，`finish_run` 无条件写 `finished_at` | 描述影响时**没去找读取方**，按「字段名像什么」推断用途。`agent_runs.status` 目前没有任何接口在读 | 结论包含「影响范围」时必须先 grep 读取方；说不清就写「暂时不可见，属数据不自洽」。已在 `TODO.md` §8 更正留档 |
| 写操作跑完，`persist_state` 只判断 `execution_status == "PENDING"`，其余一律 `COMPLETED`——工具执行失败、用户取消、审批过期全被记成「完成」 | **错误的方向是单一的**：凡是想不清楚的都落回默认分支，而默认值是「成功」。于是失败率与 p95 都不会报警，看板上一切正常。修「运行停在等审批」时才暴露：收口写进去的 `FAILED` / `CANCELLED` 会被下一次 `finish_run` 覆盖回 `COMPLETED` | 终态推导抽成纯函数 `nodes.run_status_for`（可单测），显式列出失败与取消的取值集合；`finish_run` 的调用方要意识到它**无条件覆盖**状态与原因，给状态补原因时别指望上游留的痕迹还在 |

### 2.3 部署与运行时

| 缺陷 | 根因 | 处置 |
|---|---|---|
| checkpointer 一直偷偷用 SQLite，`.env` 里的 `CHECKPOINT_DATABASE_URL` 从没生效 | 代码读的是旧变量名 `CHECKPOINT_DB_PATH`（未配置）→ 静默降级 | 非 PG 连接串在非测试环境**启动即报错**；`/ready` 的检查改为真实 `SELECT 1` |
| `langgraph dev` 进程里 `prv_*` 工具一个都没有 | Studio 只导入 `app.agents.studio:graph`，**从不执行 `main.lifespan`**，而 MCP 工具在 lifespan 里注册 | `studio.py` 加 `_ensure_mcp_tools()`（异步锁 + 标志位，失败只记日志）；症状 `tool_rejected` 写进 `docs/STUDIO.md` 排障表 |
| 趋势 CSV（420K）与审计 jsonl 在容器层，重建容器即丢 | 部署没区分「代码」与「数据」，旧容器只挂了两个 json | 先 `docker cp` 到宿主 `/var/lib/prv/{trends,exports,reports,logs}` 并 `chown 10001:10001`，再挂进容器 |
| `uv sync` 把 Studio CLI 卸掉，Studio 服务随即起不来 | 依赖组 `studio` 不在默认同步范围 | 服务器同步一律 `uv sync --group studio` |
| pandas / 沙箱副本等仓库外文件长期漂移，`install.sh` 不管它 | 「手工 scp 也算部署」 | 纳入 `deploy/install.sh`（幂等），或至少写进 `docs/REMOTE_AUDIT.md` 的复查清单 |
| 加了列的迁移没跑，服务端照样起来，直到有人写观测才 500 | `sync.sh` 默认只推代码不迁移，`--migrate` 要另外跑；而缺列这件事**启动时完全不报错** | 带迁移的提交，部署命令固定写成 `sync.sh --migrate --restart`；判断依据是「这次 diff 里有没有 `alembic/versions/`」 |
| `CONTEXT.md` 写着「`context.compact` 的 `content.output` 存摘要全文」，线上查出来是 NULL | 设计稿先于实现写成了既成事实。写的时候是有这个打算，实现时只落了 attrs；界面上又看不到摘要，两边就一直没人对账 | 设计稿里的「已实现」类描述，落地后**按字段抽查一次真实数据**。这次是 V3 做呈现时才发现，顺手补上 `span_handle.content(output=body)` |

### 2.4 智能体推理回路

这一组是 2026-11-04 线上事故的产物：带图提问 → 504 → 后端继续空转 45 分钟、521 次模型调用。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 带图提问第二轮**必定**失败：`Additional properties are not allowed ('image_id' was unexpected)` | `plan_request` 无条件 `merge_slots(state["planned_arguments"], plan.arguments)`，把上一个工具已确认的槽位拼进新工具的参数。OCR 的 `image_id` 就这样进了站点查询工具，而它的 schema 是 `additionalProperties=False` | 槽位只在**同一工具**之间继承（多轮补参需要）；换工具就只用本轮参数。断言锁在 `tests/unit/test_slot_carryover.py` |
| `plan_request ↔ execute_read_tool` 无限循环，永不收敛 | 失败分支只返回 `final_response`；而 `tool_observations` / `tool_steps` 在 `AgentState` 里**没有 reducer**，返回字典不带这两个键就等于「本轮从未发生」。路由读到的仍是上一轮的成功记录（判定可以继续），步数也一直停在原值（上限永不触发） | 失败统一走 `_round_failure()`：写一条 `success=False` 的 observation 并把步数 +1；observation 带 `round`，路由校验 `round == tool_steps`，对不上说明本轮没落地，直接收尾 |
| nginx 第 180 秒返回 504 之后，后端仍在跑 | 客户端断开不会取消服务端协程，而链路上没有任何总时限 | `asyncio.timeout(CHAT_TIMEOUT_SECONDS)` 包住 `graph.ainvoke`；超时后自己 `finish_run` 并补一条 assistant 消息，用户刷新能看到「已中止」 |
| 进程重启后留下永远 `RUNNING` 的运行记录 | 协程随进程消失，`finish_run` 永不执行；`agent_runs` 挂了 RLS，无租户上下文的 UPDATE **影响 0 行且不报错** | 启动时 `interrupt_stale_runs()` 逐租户标记为 `FAILED / process_restarted` |
| 循环里的工具失败完全静默 | 异常被 `except ToolGatewayError` 转成一句给用户看的文案，既不记日志也不记审计 | 失败分支打 `agent_read_tool_unavailable` warning，带 `tool_name` / `error_category` / 步数 |
| PDF 识别上限 120s 与请求总闸 120s **相等**：大 PDF 必然死在总闸上 | 两处超时是分两次加进来的，各自都「合理」，没人把两个数放在一起比。工具自己的超时被上层总闸盖住，等于没设 | 约束写成不变量：总闸必须**明显大于**单工具上限（160s vs 120s），再叠一层 nginx（240s）必须大于总闸。三层关系写进 `docs/OCR_INTENT_DESIGN.md` §6，改动任意一层都要重算另两层 |
| 改了 `client_max_body_size` 却没改 `X-Frame-Options`，PDF 预览整块空白 | 只盯着「传得上去」，没想到「显示得出来」需要另一条完全不同的策略（同源内嵌） | 附件功能上线前按「提交 → 存储 → 回取 → 渲染」四段各问一遍「这条链路上还有哪个配置会拦我」；帧头 `DENY` 改 `SAMEORIGIN` 并注明原因 |
| 同一会话先传图片、再传 PDF 问「核对这个报告」，模型答「附件识别结果与文件名不符」，而 PDF 一次都没被识别 | **单轮状态没归零**。LangGraph 把 checkpoint 里「本轮输入未提到」的通道原样带进新一轮：上一轮的 `tool_observations`（图片 OCR 原文）、`tool_steps`、`planned_*`、`final_response` 全被沿用。`_render_observations` 把它拼进本轮的 `user_message`，模型于是把旧图当成本轮附件；`_repeats_last_observation` 还会「同工具同参数」直接回放旧结果。整轮 3.8 秒答完，`prv_list_stations` 也没真跑 | 入口加 `begin_turn` 节点，每轮归零单轮字段（只有上一轮「缺槽位」时保留 `planned_*`，多轮补参靠它）；`[已执行工具结果]` 改名 `[本轮工具结果]`；OCR 结果里标注「该句柄不属于本条消息」，避免「读错文件」被当成「文件内容不符」。回归测试 `test_second_turn_does_not_reuse_previous_ocr_result` 用「先图后 PDF」两轮锁住，禁用重置即失败 |
| 上线后 `conversations.last_prompt_tokens` 一直是 0，压缩条件永远不满足，而日志里没有任何报错 | 用量写在 `ContextVar` 里。模型回调由 LangChain 在自己的 asyncio 任务里执行，**子任务里 set 的值不会传回调用方**，`chat_service` 在图返回后读到的是初始的 `None`。测试全绿是因为假规划器根本不产生用量 | 改成每次调用新建 `UsageCollector`，通过 `ainvoke(config={"callbacks": [...]})` 挂上去，用完按 `run_id` 暂存给 `chat_service`。`test_runnable_planner_gets_usage_collector` 锁住「必须挂上」；真机验证看 `last_prompt_tokens > 0`，而不是看测试绿 |

### 2.5 权限判定的分散与传递

这一组都是 2026-11-11 加「构建助手」时暴露的：功能本身没问题，坏在判定散在多处、身份在途中被丢。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 平台管理员（无 `agent:write` scope）在对话里让助手改配置，助手回「我没有权限」；同一时刻他点控制台按钮是能改的 | 路由层按 `is_platform_admin or agent:write` 放行，工具目录只认 scope。**同一个口径写了两遍**，改的时候只想起一处 | `ToolExecutionContext.is_platform_admin`，判断收进 `ToolGateway.missing_scopes()` 一处 |
| 修完上一条，写工具仍然一调用就被拒，报的还是「缺少该工具所需的权限」 | `_has_access`（目录过滤）与 `_check_permissions`（执行拦截）各写了一遍 scope 判断，只改了前者。现象是「目录里看得见、一调用就被拒」，错误消息看着像权限配错，把人往配置方向带 | 同上，两个入口共用 `missing_scopes()`。**同一判断不许分两处写** |
| 审批卡正常弹出，点确认返回「当前身份已不具备该操作所需的授权」 | 执行前会重建一份上下文（补审批号、边界快照），原来是**逐字段抄写**，`is_platform_admin` 没抄过去。`conversation_id` / `attachment_ids` / `credential` 也是漏的，只是这条路径没用到 | `dataclasses.replace(context, ...)` 从调用方派生。以后加字段自动带上，不用记得来这里补一行 |
| 助手为「删除内置智能体」生成了一张确认卡，人确认后才报「不能删」 | 校验只写在 handler（执行期）里。卡片出现在校验之前，**这张卡本身就在暗示这件事可能成功** | `ToolDefinition.preflight`：写路径在生成审批单前调用，返回理由则不进审批。handler 里的校验保留为权威判定（执行时状态可能已变） |

### 2.6 委派：子集语义与链条记账

2026-11-12 实现 A2A P0 时暴露的四个坑。前两条是「文档里的等式照抄会错」，后两条是「同一份记录有两个写入点」。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 设计稿写「有效权限 = `人 ∩ A ∩ B`」，落地时发现 B 没配清单就推不出这个等式 | 等式只在 B **显式收窄**时成立。`B.allowed_tools = None` 表示「我不额外收窄」，此时 `人 ∩ A ∩ B` 是 `人 ∩ A`，运行时却是 `人 ∩ B`。**两种实现语义不同**：下传交集能让等式恒成立，但会把 B 需要的工具一起砍掉，委派当场失去意义 | 用 B 自己的边界，`None` 放行；真正守住的是 `can_write` 单调（只读 A 不能转交可写 B）。推导修正写进 `A2A.md` §4，理由写进 `AGENT_CONNECT.md` §5 |
| 委派次数与路径一度准备放 `begin_turn` 清零，深度限制实测不生效 | 子调用会跑**同一个**执行图，`begin_turn` 在子图里也执行一次，把父级刚写进去的链抹掉。看起来「深度限制无效」，实际是记录被自己清了 | 每轮归零放入口（`chat_service` 构造状态时写空值），`begin_turn` 不碰委派字段。理由写成注释，否则下一个人会「顺手」把它加回 `_PER_TURN_FIELDS` |
| `A→B→A` 没被环检测拦住 | 链里只记「被转交过谁」，发起方的名字不在其中，于是 B 转回 A 时判定「没进过」 | 链从根节点开始（`[A, B]`），`target == 当前 agent` 与 `target in chain` 两条一起判 |
| 集成测试断言「用户会看到模型转述」，实际看到的是工具的错误消息 | 只读工具失败时路由**直接收尾**（不重试第二次，`route_after_read_tool`），`_round_failure` 用的是工具 `message`。所以拒绝理由本身就是给人读的那句话，测试断言该盯着它 | 拒绝文案写成完整人话（点名具体工具），测试断言这条文案而不是后续模型输出 |

### 2.7 连接器：默认值、外键与租户墙

2026-11-12 实现外部连接器时暴露的四个坑。前两条是**默认值选错**，后两条是**边界画错**。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 批量接入远端工具时，风险等级留空落成 `read` | 单条接入时人会逐个判断；批量接二十个时没人读第二十遍。默认 `read` 意味着远端一个 `delete_*` 接进来就是**免审批直接执行**，而它的名字和别的工具长得一样 | 默认改成 `write`（`BIND_DEFAULT_RISK`）。判据：**两种默认值的代价不对等时，取代价小的一侧**——多点一次确认 vs 一次不该发生的写操作 |
| 凭据缺失时最容易想到的实现是「回退到 `.env` 或行上的 `token`」 | 回退能让「忘了配」表现成「用别人的账号调通了」，而返回值完全正常 | fail-closed：不回退，直接 `CONNECTOR_CREDENTIAL_MISSING` 并写明下一步。判据：**失败要能被看见，不能被补偿掉** |
| 删连接器时打算一次清掉所有租户的凭据，运行时报 `TenantIsolationError` | 凭据表有 RLS，清理必须逐个租户开 `tenant_scope`，而请求路径上禁止跨租户切换。这不是代码风格问题，是**RLS 决定的架构**：跨租户只能走维护任务的逐租户循环 | 删除接口只清当前租户（`purge_credentials`），其余由维护任务扫（`purge_orphan_credentials`）。孤儿子行读不到（拿不到 `connectors` 行），多躺一轮不影响正确性 |
| `connector_credentials.connector_id` 没有外键，删连接器后密文留在库里 | 加外键在 SQLite 上是**空操作**（默认不开 `foreign_keys`），测试全绿也说明不了生产行为。同一取舍在 `custom_tools.connector_id` 上已经用过一次 | 显式删除，并写测试盯住「删完查不到」。判据：**外键不能是唯一防线**，尤其是本地与生产用的不是同一个库 |

### 2.8 引用传递：三态、部分成功与「凭什么」

2026-11-12 实现 A2A P1 时暴露的四个坑。共同点是：**每一种错写法都会让所有 P0 用例保持绿色**——
因为它们在「正常情况」下行为完全一样，只在出错时才分叉。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 材料读不到时返回空内容，A 完全看不出「没送到」 | 「读不到」与「本来就没有」在返回值上同形。这与 RLS 那个坑是同构的：裸 SQL 查带行级安全的表永远 0 行，「清干净了」和「看不见」长得一样（§3 第 5 条）。B 会照着「什么都没有」作答，错误被静默传播，日志一个字都没有 | 三态硬分开：可见 / `REF_NOT_VISIBLE`（带 `missing_scope`）/ `REF_GONE`。判据：**失败与空值必须在类型上可区分**，不能共用同一个返回值 |
| 一次传 5 个引用、1 个不可见 → 打算「跳过坏的那个继续」 | `except: continue` 更短，而且看起来更「健壮」。实际是让 A 以为 B 看到了全部，而 B 只看到 4 个还会给出「完整」结论——**比直接失败危险得多** | `resolve_refs` 遇错即抛，调用方整单拒绝、子图不跑。可证伪的用例：`test_one_bad_ref_rejects_the_whole_delegation` |
| 把产物的对象键递给子调用，想让 B 直接读 | 子调用的附件读取只认「本租户 + 本会话目录里的句柄」。递键过去，B 拿到一个读不了的值，报错是「附件不存在」——比不给还难查。`object_store.py` 第二行就写着「路径不是权限」 | 复制成子会话的附件（`_materialize`），只对 PDF/图片做，其余退化成只给元数据。代价是转交三次三份文件，由会话清理回收 |
| 「无证据结论不过桥」的判据一开始写成「必须有 `citations`」 | 那会把「基于 A 给的材料作答」整类正常转交一起拦掉，而这是最该工作的一类。方向错在**拦得太宽**，而测试只覆盖了「该拦」的那一边，所以发现不了 | 判据改成**三样都没有**（没调工具 + 没说缺什么 + 发起方也没给材料）。同时补反向用例 `test_same_answer_passes_when_materials_were_given`——**只测拦截不测放行，阈值收得再紧也不会被测试发现** |

还有一条不是缺陷，但值得记：**判定顺序要写死成「纪律 → 目标 → 边界 → 引用」**。
前三条都是「这次转交根本不该发生」，引用是「转交成立但材料给不过去」。
反过来写，一次越权转交会先花几次数据库查询去解析材料，然后真正的原因（边界太宽）
被藏在第二条错误里。

### 2.9 委派写操作：中断的位置、跨线程的结论、两跳的复核

2026-11-12 实现 A2A P2 时暴露的四个坑。前两条是**机制**问题，后两条是**判据**问题。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 父图也想跟着子图的审批一起中断 | 审批靠「图在审批节点中断、恢复后从断点继续」，而子调用发生在一个工具处理函数内部。让父图也停在那里，恢复时整个委派会被重放一遍——于是「同一次委派跑两遍」或「审批被静默跳过」。P0 干脆强制只读，就是为了绕开它 | 不去嵌套：子图的 `__interrupt__` 被读成一个普通工具结果（`status=awaiting_approval`），父图照常收尾；子图的检查点**留到**确认 / 取消 / 过期才丢。判据：**中断不能跨调用栈冒泡时，把它变成一个返回值**，不要把栈压平 |
| 确认后结论写进了子线程，用户那边永远停在「请确认」 | 恢复用的是 `approval.thread_id`（子线程），写消息也沿用了它；而 `_append_assistant` 按 `thread_id` 找会话，子线程不对应任何会话，于是只留一行「定位不到会话」的 warning。接口返回 200，看起来一切正常 | 审批单多存一列 `parent_thread_id`：**恢复看 `thread_id`，落结论看 `parent_thread_id`**。判据：跨线程的动作要显式区分「在哪执行」与「结果归谁」 |
| 上游收窄了，单子照跑 | 复核写成「B 还可用吗 + B 的清单变没变」——这只覆盖单跳。A→B→C 里 A 被收窄时 C 看起来完全合法，而那张单子已经是越权链的产物的产物 | 存**整条链**、从直接上级往根逐跳 `子 ⊆ 父`；错误码单列 `DELEGATION_REVOKED`（与 `AGENT_UNAVAILABLE` 分开，否则排障会去查一个没问题的 B）。可证伪的用例是两跳那条：去掉循环、只比直接上级时必须红 |
| 反向验证的断言太弱，删掉实现用例仍然是绿的 | 断言写的是「会话最后一条里出现『运行值班』」和「metadata 里出现 approval_id」——**模型自己那句话和等待确认那条消息里都有这两个词**。把 `persist_thread_id` 改回 `thread_id`（即结论不回写），用例照样通过 | 断言要指向**只有被测实现才能产生**的东西：消息条数从 2 变 3、最后一条带 `approval_action=confirm`、且不含「需要你确认」。判据：**做完反向验证再看一眼断言**——如果做不到「去掉实现就红」，这条用例的价值是负的（它给出了已经测过的错觉） |

还有一条不是缺陷：**委派来的写操作，结论不经过发起方转述**。确认发生在子线程上，
B 的执行结论直接贴进发起方的会话。这是「不做第二次确认」的代价，与 `A2A.md` §6 的
立场一致——给委派也加审批，人会开始闭眼点。

### 2.10 入库与检索：两个「看着一样」的坑

2026-11-16 把 GB 27791 / GB 27790 交给 PAP 时暴露。两个都不是逻辑写错，
而是**同一份东西在两个地方长得不一样**。

| 缺陷 | 根因 | 处置 |
|---|---|---|
| 同一份 27791 在库里两份（25305 / 24696 字节，各 48 片） | 内容哈希建在 `text.strip()` 上，而 `\r\n` 与 `\n` 是不同字节。磁盘那份 `.md` 是 Windows 写的 CRLF，`pap_push` 内存里现转的是 LF，于是「重复上传不重复入库」失效 | `extract_text_from_bytes` 统一把 CRLF/CR 折成 LF；单测断言「CRLF 与 LF 解析结果相等」。判据：**幂等键必须建在归一化之后**——行尾、BOM、全角空格都算格式差异，不算内容差异 |
| 27791 入库后问「过滤精度和初始压损」永远 `knowledge_not_found` | 召回第一名本来就是对的（《5.1 一般要求》score 33.3），是重筛把 12 条全删了：`rerank.MAX_PROMPT_CHARS` 是 500，而答案在第 **507** 字。切片上限 800，两个常量早已脱钩，注释却还写着「比摘录 400 字看得更全」 | 上限提到 900，并加单测盯「重筛上限 ≥ `ingest.CHUNK_SIZE`」这个耦合。判据：**模型看不见的部分等于不存在**——凡有截断，就要有一条「截断点不会落在正文中间」的测试 |

两条的共同点是**没有报错**：一条静默多出一份副本（`created` 而不是 `unchanged`），
一条静默答不上来（`tool_error=knowledge_not_found` 看起来像「库里没有」）。
所以排查顺序是**先看数据长什么样，再看检索为什么剔**：
`psql` 查 `length(content)` 与目标词的 `strpos`，比读代码快得多。

### 2.11 一次字段改名的三处连带

2026-11-16 把 `tenant_tool_policies.enabled` 换成三态 `action` 时，三次报错都不在被改的地方：

| 现象 | 根因 | 处置 |
|---|---|---|
| 管理接口 500，`TypeError: set_tool_policy() got an unexpected keyword argument 'enabled'` | 改了服务签名，**没跟着改第二个调用方**（`/admin/tenants/{id}/tools/{name}`）。它和新接口是两条独立路径，单测只覆盖了其中一条 | 老接口保留 `enabled` 入参并映射到 action（`enabled=false` → `deny`），三态走新增的 `action` 字段。**改签名先 grep 调用点**，别只改自己想到的那处 |
| 运行配置接口 500，`Extra inputs are not permitted: default_tool_action` | `runtime_settings.snapshot()` 多返回了键，而 `RuntimeSettingsResponse` 是严格模型（多一个字段就 500）。快照函数与响应模型没有类型约束把它们绑在一起 | 响应模型补四个字段。**给共享 snapshot 加键之前，先看它被哪个严格模型接着** |
| 单测 `_FakeGateway` 报 `no attribute 'requires_approval_for'` | 产品代码从 `requires_approval(tool)` 换成带上下文的判定，测试替身没跟。替身只实现了「当时用到的方法」 | 给替身补同名方法。**调用的方法名就是替身的接口契约**，改产品侧签名等于改所有替身 |

共同点：三处都是「改了一个事实，忘了一个读者」。判定唯一化（把审批判定收进
`approval_required` 一个函数）解决的是第一类的一部分，剩下两类只能靠改完立刻
跑全量测试——它们恰好都在全量测试里红了，没有一条逃到线上。

## 3. 验证类假象：看到的不等于发生的

这一节是全篇最有价值的部分。共同特征是：**测试通过、代码看着对、接口返回 200，但功能是坏的**。

| 看到的 | 实际 | 现在的做法 |
|---|---|---|
| 本地 175/372 项测试全绿 | SQLite 不校验长度约束、唯一约束、RLS | 这三类只在真实 PG 上验。规律写在 `docs/MULTI_TENANCY.md` |
| 测试全过、代码 review 也过 | 从没跑过真实对话 → 思考模型 + 强制 `tool_choice` 必返回 `400 Thinking mode does not support this tool_choice` | 规划器主动下发 `reasoning_effort="none"`；每个功能至少一条真实链路验证 |
| `_classify` 里明明有 HTTP 状态分支 | `getattr(exc, "status_code", None)` 对 `httpx2.HTTPStatusError` 永远 None（该类只有 `.request` / `.response`），分支是死代码，所有鉴权失败都被报成 `MCP_PROTOCOL_ERROR` | 改读 `.response.status_code`；**判断「代码里有没有这个分支」要跑一次坏输入，不能靠读** |
| 工具列表在、`/tools` 返回 200 | 严格鉴权打开后可能根本调不通 | 端到端读到「进口压力 1.538 kPa」才算通过 |
| 线上静态文件 md5 与本地不一致 | 本地工作区是 CRLF，服务器是 LF；`git cat-file -p HEAD:path` 三处 blob 完全一致 | 比 blob 不比工作区文件（见 §4） |
| `test` 租户 403「当前用户不属于该租户」 | 该租户成员叫 `test-user`，我用的是 `pap-admin` | 先 `user_roles join users join tenants` 查一眼，再怀疑代码 |
| 控制台点击后截图为空/旧状态 | `--screenshot` 在 load 后立刻截，`setTimeout` 来不及跑 | 加 `--virtual-time-budget=4000` 截异步结果 |
| 图表/趋势显示「已完成」但没数据 | 对端某站 `overview` 不返回 `success` 字段，平台按「无 success 视为成功」处理 | 缺字段要当「未知」而不是「成功」 |
| 日志里一条 ERROR 都没有，模型调用全是 200 OK | 循环里模型每次调用都成功，只有工具在静默失败（异常被 catch 成一句给用户看的文案，不记日志也不记审计） | 排查先看调用**频率**和间隔，别只看错误级别；失败分支必须落 warning 与 `error_category` |
| 迁移 SQL 在 psql 事务里跑通、结果正确、已回滚 | 那是**字面量**版本；`alembic` 走的是绑定参数，`INSERT ... SELECT` 投影里的 `:new` 是无类型 text，与 `varchar(128)` 不兼容 → `text versus character varying`，迁移中途失败、服务停着。（2026-11-05 实际发生） | 验证迁移必须跑**即将上线的那份代码**，不是它的手工改写版：先 `alembic upgrade --sql` 或直接在容器里用 prepared statement 带参数跑一遍；字面量通过只能证明「思路对」 |
| 新表 671 项测试全绿，迁移也跑过了 | 首次 `INSERT` 就 500：`invalid input value for enum agent_status: "ACTIVE"`。`Enum(AgentStatus)` 默认按**成员名**落库，而手写迁移建的 PG 原生枚举标签是小写 value。SQLite 两边都按成员名，自洽得看不出问题（2026-11-11 实际发生） | 原生枚举列必须带 `values_callable=lambda e: [m.value for m in e]`；回归用例**读裸 SQL** 断言列值，ORM 属性会把两种写法都还原成同一个成员。同类问题在 `tenant.status` 已踩过一次，注释就在那里 |
| `/chat` 返回 `status=awaiting_approval`，审批预览里的参数也都对，于是判定「写路径通了」 | 写操作在**确认之后**才真正执行，而执行走的是另一条入口（`execute_approved`）。卡住点恰恰在这条入口上的权限判断，提案阶段一路绿灯（2026-11-11 实际发生） | 写路径的冒烟必须走到 `confirm` 并查库看结果，`awaiting_approval` 只证明「目录与提案放行了」。同时冒一次 `cancel` 确认无副作用 |
| 连接器页截图「看着正常」，点开某张卡的表单才发现挤成一团 | 卡片只有 300px 宽，而表单是两列：长标签折成两行、下拉被压成半截。**截图只看静态首屏，验不出展开态**；`node --check` 也只验语法不验布局 | 无头 Edge 用 CDP 点按钮再截：`Runtime.evaluate` 触发 `click()` → `Page.captureScreenshot`。顺带这一招还能证明**事件委托真的接上了**（按钮是 `data-act` 冒泡到根节点处理的，纯截图看不出按钮是否可点） |
| 同一行里一张卡展开面板，另外两张被拉成同样高、底部一片空白 | Grid 默认 `align-items: stretch`，行高由最高的那张决定 | 卡片网格加 `align-items: start`。判据：**可展开的卡片放在网格里，一定要显式处理行高拉伸** |
| 写操作被拦下了：`error_code=DELEGATION_REVOKED`、`success=false`、数据库里 `executed_at` 为空，判定「这条验收通过」 | 同一段话的第一句是「已完成你确认的操作」——前缀跟着「用户点了确认」走，而不是跟着执行结果走。状态字段能证明拦截发生了，证明不了它没被说成成功（2026-11-12 实机） | 失败路径要**连文案一起断言**：「已完成」不该出现在失败路径上。判据：状态字段与人对外的第一句话是两个交付物，只验前者等于放走后者 |
| 静态预览里输入区「就是一张干净的卡片」 | 全局 `textarea:focus` 带着 `box-shadow: 0 0 0 3px` 浅绿光晕，卡片聚焦时又画一圈外框 → 实际是**双层框**。未聚焦的空态预览完全看不出来，是用户截图才暴露的（2026-11-16） | 自定义输入壳必须把子控件的 `border / outline / box-shadow` 显式清零，并截**聚焦态**验证；只截空态等于没验 |
| 抽屉里点一下，红条一闪而过，判定「提示给过了」 | 那是 `toast`：三秒后自己消失。用户回头核对「刚才那份文件为什么没进去」时，界面上什么也没留下——**提示存在过**与**用户能查到**是两件事（2026-11-16） | 交互里产生的报错与提示落进对话流（独立卡片、带时间、可复制），不用一闪而过的浮层；只对「操作成功」这类无需回看的结果继续用 toast |
| 用户问「为什么还是不让传 markdown」，而代码里刚把「只收图片 / PDF」当规则写下来 | 一个附件类型要同时穿过**四层**：前端 `accept` + 类型校验、服务端白名单 + 内容判定、读取工具、托盘与气泡展示。只改一层时表现各不相同：选不出来 / `rejected` / 上传成功但模型读不到 / 历史里显示成一个图片框。而上一层「看着正常」会让人以为整条链路通了（2026-11-16） | 加类型按四层逐项核对。文本没有文件头，判定只能「后缀白名单 + 形态检查（NUL / 控制字符 / 可解码）」，不能信 `File.type`（浏览器对 `.md` 常给空串）。类型清单只维护一处（`_ALLOWED_MIME`），大小上限与产物转交都引用它，别各抄一段 `if/else` |
| 「附件活一轮就删」这条规则单测全绿 | 它把**反问**也算成了「一轮结束」。线上实录：295KB 国标传上来，助手问「入库方式要先定」，用户回「？」，助手答「附件不存在或已过期，请重新上传」——同一份文件在**一问一答之间**被删掉。规则本身没错，错在「一轮」的边界画在了 `finally` 上（2026-11-16） | 先按「这一轮怎么结束」改：反问轮（`needs_input`）不收，暂留到用户下一句。一天后意识到更根本的问题——**「活一轮」本身就是错的边界**：用户传一份规程、先问两句、隔一轮再说「按第三章入库」，文件照样会没。2026-11-17 改成会话材料，只有删除会话才回收。判据：**资源该活多久，看用户拿它干什么，不看函数在哪返回** |
| 点完确认，气泡里冒出一段 `{"filename": …}`，刷新就没了 | 那段 JSON 是前端当场把 `r.result` 拼进 `msg.text` 的产物，只活在内存里；而真正的结论（片数、加工链路）是服务端另存的一条消息，刷新后才出现。**当场与刷新后是两种东西**，用户只能理解成「数据丢了」。同一批消息里的结论还带着同一个 `approval_id`，还原历史时又长出一张一模一样的审批卡——当场只渲染了发起的一条，所以只有刷新才看得见（2026-11-17 用户截图） | 结论按服务端的样子渲染成独立气泡，卡上只留状态（`success=` / `verified=` 这类字段名不进卡）；同一张单子只认「发起」那条消息（`approval.js` 的 `cardOwners`，两个聊天入口共用）。判据：**当场渲染出来的每一段文字，刷新之后都要能原样找回来**；做不到就别在界面上临时拼

## 4. 可复用的验证手段

这些都是踩坑后摸索出来、验证过确实能提前发现问题的做法。

**本地跑真实前端交互**（无构建、无浏览器自动化框架）：

```
SQLite + APP_ENV=test（进程内 checkpointer）
  + 自制反代注入 X-Authenticated-Subject: pap-admin
  + 无头 Edge（--remote-debugging-port）+ CDP 驱动真实点击
```

`APP_ENV=development` 会强制要 PostgreSQL checkpointer，本地起不来；`test` 才用 `InMemorySaver`。

**验证对端鉴权改动而不动生产**：用**原镜像挂载新代码**单起一个容器——
`--network host`、换端口（8766）、打开目标开关、限制 `PRV_ALLOWED_HOSTS`，
再让客户端连它。比本地造假服务端可靠，且不碰生产端口。

**比对线上与仓库是否一致**：

```bash
git show HEAD:path | md5sum                                  # 仓库里的真实内容（恒为 LF）
ssh server "md5sum /path/file"                               # 线上
```
直接 `md5sum` 工作区文件在 Windows 上会得出假结论。

**排除环境变量干扰**：MCP 客户端必须 `trust_env=False`，否则进程里的 `HTTP_PROXY` 会劫持内网调用。

**清理探测现场**：按 `thread_id like '%:probe-%'` 先删 `messages` 再删 `conversations`，
避免验证数据污染会话列表。审批、供应商等临时对象同理。

**绕开 nginx 基本认证直接冒烟接口**：`/etc/nginx/pap.htpasswd` 是哈希，
口令丢了就问不出来；`/root/pap_app_password.txt` 里那份也不一定对得上（2026-11-06 就不同步）。
`auth_mode=gateway` 只按 `X-Authenticated-Subject` 查库、不校验来源 IP，
所以在服务器上直连应用端口、自己注入身份头即可：

```bash
ssh pap 'curl -s -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-Authenticated-Subject: pap-admin" -H "X-Authenticated-Tenant: default" \
  -d "{...}"'
```

**写路径要走到 confirm**：`awaiting_approval` 只证明提案放行，执行是另一条入口
（`execute_approved`）。一条完整的冒烟是 `chat → confirm → 查库`，
再补一次 `cancel → 查库无变化`。做一件事分两次调用，中间那次「成功返回」
很容易被当成做完了。

**查线上库要先把 scheme 改成 psql 认识的**：

```bash
URL=$(sed 's#postgresql+psycopg#postgresql#' /root/.pap-admin-db-url); psql "$URL" -c '...'
```

`.pap-admin-db-url` 里是 `postgresql+psycopg://`，`psql` 解析不了这个 scheme，会**静默退回
本地 socket** 并报 `No such file or directory ... .s.PGSQL.5432`——看着像「数据库没起」，
其实连都没连对。改完走 TCP（`127.0.0.1:5432`）。

**上线后的接口冒烟用可信头，写完还原**：线上没有可用的登录凭据时，`X-Authenticated-Subject`
/ `X-Authenticated-Tenant` 就是最直接的通道（`scripts/eval_knowledge.py --mode http` 用的同一套）。
查库里的真实用户与角色，别凭空编 subject：

```sql
select u.external_subject, r.name from users u
  join user_roles ur on ur.user_id=u.id join roles r on r.id=ur.role_id;
select user_id, tenant_id, station_id from station_grants;   -- 用户属于哪个租户
```

顺序按「普通成员读 → 普通成员写（应 403）→ 管理员写 → 查库确认 → 还原 → 删行」走：
**写生产配置属于修改线上数据**，验完必须把行删掉（测试插入的行会让下次「未配置」断言失真），
再做一次 `select count(*)` 确认归零。

**别自己发明检查口径**：这个仓库的 lint 门禁是 `uv run ruff check .`（无 CI、无
`.pre-commit-config.yaml`），**不是** `ruff format`。全量 `ruff format --check .` 会报
`app/agents/graph.py`、`slots.py`、`api/routes/admin.py` 等一批文件——它们 `git diff HEAD`
无差异，是既有代码从未格式化过，不是你引入的。正确的做法是：`ruff check .` 必须全绿，
`ruff format --check` 只跑**本次改动的文件**。否则要么误判自己搞坏了仓库，要么顺手重排
一批无关文件让 review 失焦。

## 5. 判据清单

发版前、写新功能前、以及 review 别人的改动时过一遍。每条都对应上面至少一个真实事故。

**数据与状态**

- [ ] 同一件事有没有在两处存状态？（审批配置 / 枚举名与值 / 迁移与 ORM / 前端与后端）——存两处必然不同步。
- [ ] 同一个**判断**有没有在两处写？（目录过滤与执行拦截 / 路由层与工具层 / 提案前与执行前）——改一处漏一处，且症状总指向错误方向。
- [ ] 手工重建的对象，字段是逐个抄的还是从原对象派生的？（`replace()` 少一次漏一个，漏掉的字段不会报错）
- [ ] 注定失败的操作，会不会先弹出一张确认卡？（让人对着必然失败的卡片点确认，训练出的是闭眼确认）
- [ ] 排序依赖的时间字段，精度够不够区分同批写入？（秒级时间戳排同秒数据 = 随机序）
- [ ] 带时区的时间读出来真的是 aware 吗？（SQLite 会给你 naive）
- [ ] 新增表/列在**真实 PG** 上跑过吗？（长度、唯一约束、RLS 都是 SQLite 测不出来的）
- [ ] 这段逻辑在**第二个租户**下跑过吗？只在默认租户跑过等于没跑过。
- [ ] 幂等键算在**归一化之后**吗？CRLF 与 LF、BOM、全角空格是同一份内容的不同字节——哈希不同，「重复上传不重复入库」就失效了。
- [ ] 凡有截断的地方，截断点会不会落在正文中间？（切片上限 800 而重筛只喂前 500 字，答案正好在第 507 字；**模型看不见的部分等于不存在**）

**边界与失败**

- [ ] 页面上的每个按钮和开关，点了真的生效吗？不生效的就别放（「停用即注销注册表」是同一类问题）。
- [ ] 状态切换后，对象还能找回来吗？（停用的工具若从列表消失，「停用」就变成单向操作）
- [ ] 外部执行的返回值，有没有独立回读核验？（不能自证）
- [ ] 有没有静默降级、静默跳过、`| tail` 吞退出码？（失败和成功长得一样，就是定时炸弹）
- [ ] 重试语义对吗？（写操作的 5xx 不代表没写进去；凭据错误重发无意义）
- [ ] 回路 / 重试的终止条件，读的是「本轮」的状态还是「最后一次」的状态？没有 reducer 的字段，不写就等于没发生。
- [ ] 多轮会话里，哪些字段属于「这一轮」？（工具结果、步数、计划参数、最终回答）新请求开始时它们清零了吗？checkpointer 会把**你没提到**的通道原样带进下一轮。
- [ ] 工具收到的标识符，能区分「属于这次请求」和「属于这个会话」吗？（能解析 ≠ 是本轮该用的那个）
- [ ] 客户端超时之后，服务端会不会继续跑？**必须由服务端自己设总时限**，客户端断开不等于协程被取消。
- [ ] 失败分支有没有留下可观测的痕迹（日志 / 审计 / 状态字段），还是只回了一句文案？
- [ ] 审计、埋点这类**旁路写入**有真实落库的证据吗？（独立会话 + 容错 except = 失败与成功长得一样；SQLite 测不出 RLS，得在 PG 上看一眼 `select count(*)`）
- [ ] 那句 `select count(*)` 是**在租户上下文里**跑的吗？裸 SQL 查带 RLS 的表永远返回 0 行——「清干净了」和「根本看不见」一模一样。清理核对要在 `tenant_scope` 里数，或者把租户条件显式写进 WHERE。
- [ ] 这个默认值，选错时的**代价对称**吗？（默认「读取」= 免审批执行；默认「写入」= 多点一次确认。不对称就选严的）
- [ ] 依赖外键做级联清理？本地是 SQLite 的话，`foreign_keys` 默认关着——外键在那里是**空操作**。要么显式删，要么先确认两边行为一致。
- [ ] 跨租户的批量操作，是不是在请求里做的？请求的租户上下文禁止切换，这类动作只能放进维护任务的逐租户循环。
- [ ] 有展开态的界面，验过展开后的样子吗？（静态首屏截图看不出表单挤压、行高被拉伸）
- [ ] 「读不到」和「本来就没有」在返回值/错误码上是**两种东西**吗？塌成一种，错误就会被静默传播——对方拿着一份空材料照样给出「完整」结论。
- [ ] 批量处理里有没有 `except: continue`？它就是「部分成功」，而部分成功对调用方等于全部成功。
- [ ] 我加了一条拦截/阈值，有没有同时写一条**该被放行**的用例？只测拦截不测放行，阈值收得再紧也不会被发现。
- [ ] 跨调用栈的中断（审批卡在工具处理函数里）是怎么处理的？把它变成一个返回值，还是硬把栈压平？后者会让恢复时整段重放。
- [ ] 一个动作的执行方与结果的归属方是同一个线程吗？不是的话，「在哪恢复」与「结果写回哪」要分别存，不能沿用同一个 id。
- [ ] 逐级判定的实现，验过**两跳以上**的情形吗？只比直接上级的版本在所有单跳用例里都是绿的。
- [ ] 这条反向验证做完之后，断言真的会红吗？断言里出现的词如果模型自己也会说，那它证明不了任何事。

**配置与凭据**

- [ ] 每个配置项都有代码读它吗？没人读的配置比没有更危险——它让人以为生效了。
- [ ] 探测/排障输出里会不会带出密钥、令牌、口令？只打印指纹。
- [ ] 凭据是否只以「环境变量名」形式落库，运行时才解析？

**文档与事实**

- [ ] 文档里的结论是实测的还是推断的？推断必须标「待验证」，实测后立刻回头改。
- [ ] 记录里的事实（路径、提交号、部署方式）是查过的还是记得的？

## 6. 已知欠账

截至 2026-11-04，以下是有意识留下、尚未闭环的项。**修掉之后请删掉对应行**，不要让清单变成历史陈列。

| 项 | 影响 | 备注 |
|---|---|---|
| `/srv/upstream-mcp` 不是 git 仓库，靠手工 scp | 无法回答「线上跑的是哪个 commit」，只能比 md5 | 建议改 `git clone` 检出 |
| 线上 `.env.bak-20261104-155636`（600） | 含已失效的 MCP 令牌 + **仍在使用的模型 Key** | 建议留几天供回滚，稳定后删 |
| `/etc/prv/tenants.json.bak.*` 两份 | 含旧令牌（已失效） | 同上 |
| 沙箱执行记录存内存，重启后回读 404 | 重启前发起的审批无法核验 | 刻意如此（不猜），换持久化时需同步改 verifier |
| jieba 首次初始化仍在首个请求里同步执行（约 0.76s） | 首请求延迟 | 可挪到 lifespan 预热 |

## 7. 代价盘点

四个最贵的故障，共同点是**都通过了测试，都没通过「真实跑一遍」**：

1. **Studio 不执行 lifespan** → MCP 工具一个都没注册，表现为「压力趋势」莫名其妙走成本地工具、再被判越权。排查成本高是因为症状（`tool_rejected`）离原因（生命周期没跑）很远。
2. **沙箱只写 handler 不写 verifier** → 状态永远停在 `EXECUTED`，功能看着通实际不可用。
3. **基于「OCR 绑公网」这个错误前提做的一轮安全结论** → 制造了不存在的事件，也让人对真实暴露面（四个存量容器）晚了一步察觉。
4. **推理回路终止条件依赖「最后一条 observation」** → 失败分支没记账，回路永不收敛；客户端早已 504，后端空转 45 分钟、521 次模型调用。它的上游还有一个更早的缺陷：**跨工具参数串味**（OCR 的 `image_id` 进了站点查询工具），让「带图提问」的第二轮必定失败。这条最贵的地方不在钱，而在**它没有制造任何错误日志**——所有模型调用都是 200，只能靠「调用频率」发现。

唯一造成外部影响的是令牌打印，已轮换并验证旧令牌 401。其余都是内部返工。
