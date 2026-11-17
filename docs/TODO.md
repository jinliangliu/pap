# 待办清单

只记「已经想清楚、但这一轮不做」的事，以及「上线前必须做」的事。
每条都写清为什么搁置、什么条件下开工——没有触发条件的条目等于许愿。

完工后从本文件删掉，细节留在对应主题文档里（技能看 [`SKILLS.md`](SKILLS.md)，
知识库看 [`KNOWLEDGE.md`](KNOWLEDGE.md)，观测看 [`OBSERVABILITY.md`](OBSERVABILITY.md)）。

## 1. ~~上线前必做（阻断项）~~ 已改判，2026-11-15

这一节原来是「发版前必须关观测 + 清数据」。**2026-11-15 起不再成立**：

- 服务器的定位是**开发调试环境**，观测是排障的主要依据，需要开着。
- 开关从 `.env` 移到运行配置（`platform_settings` 的 `observability.enabled`）：
  控制台「运行配置 → 观测」由 `platform_admin` 决定，**改完立刻生效、不重启**。
  `.env` 的 `OBS_ENABLED` 只作为首次默认值（现在默认 `true`）。
- 所以「上线前关掉」不再是动作，而是管理员的一个日常选择。想关就关，
  已经落库的数据不受影响、按保留期清理。

历史记录（那次确实执行过，留档在服务器 `/root/pap-obs-backup-20261115.json.gz`）：
`.env` 改 `OBS_ENABLED=false` 并重启，`obs_spans` / `obs_traces` 各删 1201 / 128 行（表保留）。
**真要清数据时**顺序不能反：先关开关再删，否则清完立刻又被写回（步骤见
[`OBSERVABILITY.md`](OBSERVABILITY.md) §11.2）。

顺带修掉清单里两条失效项：关闭态原来**不打任何启动日志**（现已补
`event=observability_disabled`，运行时切换也会打），文档里的
`GET /observability/status` **不存在**（已改为用 `/observability/overview` 的
`enabled` 字段判断）。

## 2. 技能 P2（待定，先看数据）

三项都**不做**在 P1 里，理由一致：没有真实命中分布就选不出该做哪一项，
先让 `skill_hits` 攒 1～2 周。

| 事项 | 做法（若开工） | 代价 | 开工条件 |
|---|---|---|---|
| 模型自选技能 | `ActionPlan` 加 `skill_name`，模型先选技能再规划；未选中回落到触发词匹配 | 每次规划可能多一轮往返，延迟上升 | 出现「触发词写不全 / 一条消息跨两个技能」的真实抱怨 |
| 技能引用知识库 scope | 技能上挂 `knowledge_scope`，命中时收窄检索范围，与 `allowed_tools` 同构 | 多一张关联表 + 检索路径加一层过滤 | 有技能明确要求「只查某几份文档」，且现在只能靠正文里写提示词约束。知识库已有文档级 `access_scope`，这是更粗的一层，不能替代「按文档 id 收窄」 |
| 命中满意度 | 用 `skill_hits` 关联后续轮次：同一线程 30 秒内的追问 / 重试用「未命中」或「不满意」标记 | 判定规则一定会误判，需要人工抽样校准 | 近 7 天命中数稳定 > 20 次/周，否则样本量不够看出差异 |

先做哪一项的判断依据：看 `obs_traces.skill_name` 与 `skill_hits` 的分布——
命中集中在少数几个技能，说明该补「满意度」；命中方式几乎全是 `explicit`（用户主动打 `/名字`），
说明 `keyword` 触发词写得不够，那优先补触发词而不是做模型自选。

## 3. 观测 M4 / M5

| 里程碑 | 范围 | 为什么搁置 |
|---|---|---|
| M4 | token 与成本（模型配置加可选单价）；错误分组；慢请求与错误率接 watchdog | 得先有稳定的真实 trace，否则单价与阈值都是拍的 |
| M5 | 14 天保留期清理定时任务；可选 OTLP 桥接；`obs_daily_rollup`（按需，不提前做） | 保留清理依赖「开关已关、数据已清」这个前提，先做第 1 节 |

M4 的顺序建议：先做成本（纯聚合，不改表），再做错误分组（要定分类口径），
最后接告警（要调阈值，最容易误报）。

## 4. 知识库 P1（切片与引用质量）

P0 解决的是「能不能管」：上传、列表、下架、重建、增量同步。
P1 原定三项与阶段 0 评测闭环已完成，只剩需要 embedding 端点的向量召回、
以及会改动线上排序的结果去重。

已完成（不在下表内）：

- 分词剔停用词与标点、阈值改相对值（`97ee5c1`）；
- 标题感知切片：按 Markdown / 中文编号标题分节，`section` 存真实标题路径，
  并把 `CHUNKER_VERSION` 纳入增量判定，老行自动重建一次；
- 引用可点开：hash 跳转 → 展开文档 → 高亮目标切片，超 50 片自动按 `offset` 翻页；
- 语义重筛：BM25 召回 12 条 → chat 模型按 0/1/2 三档判「是否提供问题所问的信息」
  → 只留 2 分、剔除字面撞词与「同主题但不回答」的假阳性 → 按原顺序取 4 条。用现有 `deepseek-chat`，**不需要 embedding 端点**，
  不改表、不重建索引；超时 / 非法输出 / 模型未配置一律回落 BM25。
  代价是每次知识问答多一次模型调用与最长 8 秒等待。
  配置 `KNOWLEDGE_RERANK_ENABLED=false` 可完全关掉。
- **阶段 0 评测闭环**（2026-11-09）：`tests/fixtures/knowledge_golden.json`
  25 条标注集 + `scripts/eval_knowledge.py`（local 纯 BM25 / http 端到端两种模式，
  Hit@k、MRR、可回答零召回率、假阳性率）；标题 / 文件名 / section 字段加权
  （默认 2/1，只证明「词在文件名里」这一条召回收益）；登记式查询同义词扩展
  （默认关闭，A/B 显示 MRR +0.024 但有一处排序回退）；补《平台能力说明》资料；
  修好 `/search` 被吞掉的 `limit`。基线数字与用法见 [`KNOWLEDGE.md`](KNOWLEDGE.md) §5。

「描述一下你的能力」的实词假阳性已由重筛解决。重筛**只治「召回到了错的」**，
治不了「压根没召回」：BM25 与查询无词面交集时返回 0 条，模型无从筛起。

**2026-11-15 查证：关键词串（如「调压箱 泄漏 通风」）返回 0 条，不是召回也不是重筛缺陷。**
当时的怀疑是「重筛拿『是否回答了问题』去判一串关键词，整池判 1 分」。实测否掉了：
同一条 12 条候选池，用严格口径与另写的「覆盖口径」各判一遍，**在库内确有相关内容时
四次比对结果完全相同**（[3,4]、[1,3]、[1,2]、[1]）；差异只出现在内容本就缺失的查询上，
那种情况下覆盖口径多留的是边缘条目，属于放宽而非修正。所以那套双判据改了一半就撤了。
真正的原因是**语料覆盖**：整份《城镇燃气智能调压箱技术规范》里「通风」只出现一次
（6.1 试验条件「通风良好」），「泄漏」六处全是顺带提及（报警数据项、通信报文），
没有任何泄漏处置或箱体通风的要求——0 条即「资料不足」的正确答案。
要改善，方向是补语料，或让编排把现场处置类问题指向对应的规程，**不是继续调检索**。

**2026-11-15 补齐证据（23 条现场问题实测）**：`空 0 · 扣 21 · 中 2`，
而那 2 条「中」都是误命中（`报警 等级 分级` 命中《型号表示方法》、
`切断阀 跳闸 复位 操作` 命中《智能控制器技术要求》），真实覆盖 **0/23**。
「空 0」是关键：BM25 靠「阀 / 压力 / 报警 / 处理」这些通用词一直能召回 5–12 条，
从来没有「搜不到」，召回来的全是产品标准条文。所以这是**语料形态问题**，
不是检索能力问题——补规程类资料后应当直接命中，不必动召回层。
要补哪些、怎么验收、哪些问题不该进知识库，见 [`KNOWLEDGE_GAPS.md`](KNOWLEDGE_GAPS.md)；
探测工具 `scripts/probe_knowledge_gaps.py` + `tests/fixtures/knowledge_gap_probe.json`。

**同日验证「补语料就能命中」**：内存预演（读生产库 118 条活跃切片 + 叠加测试规程，
不写库）三组对照——基线 **0/4**，补规范写法 **4/4**（留 4/2/1/1），
补坏写法（整段一块、`section=正文`、无型号）**也是 4/4**（各留 1）。
两条结论：① 召回层不用改，「上向量检索」对现场类问题可以放下；
② 切片形态不影响**能不能**拿到内容，只影响拿到的是「4 条独立步骤」还是「一坨整块」。
所以补录规范的理由是「补了能照着做」，不是「不遵守会白补」——后者已被实验否掉。

| 事项 | 做法 | 代价 | 为什么现在不做 |
|---|---|---|---|
| 向量召回（真·混合检索） | embedding 供应商或本地句向量模型，BM25 + 向量做 RRF 融合，再交叉编码器重排 | 新依赖 / 新端点 + 每次入库的 embedding 成本 + 一张向量表 | 服务器只配了 `deepseek-chat`，DeepSeek 没有 embedding 端点；要引入本地模型或第二家供应商。当前语料 7 篇 / 118 切片，假阳性问题已由重筛兜住（线上假阳性率 0），加向量的边际收益还看不出来 |
| 结果来源去重 | `top_k` 内每份文档最多 1–2 条，或加 MMR 多样化 | 会改线上排序，必须重跑 golden set 与线上 A/B | golden set 里只有 `idempotency-key` 一条因此落榜（`notes/使用说明.md` 四个切片占满 top4）；线上 http 评测同一条也是 12 条候选全被剔除。等真实语料上量、出现「同一份文档刷屏」的抱怨再开工 |

开工条件：真实文档数量上到几十篇、或出现「明明有这份资料但搜不到」的具体抱怨
（注意与「搜到了错的」区分，后者重筛已经处理）。
判断依据看 `/knowledge/search` 的 `found=false` 比例与 `citations` 的 score 分布。
**已经有一条实证**：线上 http 评测里「你会做什么」在补了《平台能力说明》之后
仍只召回 3 条、全部被重筛剔除，`answerable_zero_hit_rate=0.0952` 里的一条就是它。
这条不该靠放宽重筛或补更多同义语料硬治，正确解法是双路召回；只是当前 25 条
标注集里只占 1 条，还不到为此引入第二家 embedding 供应商的收益阈值。

另外一类抱怨不要靠调重筛口径解决：问「描述一下你的能力」这类**开放式元问题**，
严口径会返回「资料不足」，根因是库里没有能力清单文档。阶段 0 已补《平台能力说明》；
以后出现新的元问题，仍然先补资料，或让规划器把它判成普通对话意图。
把提示词调回「沾边就保留」只会让假阳性回来。

## 4.5 智能体平台化（已完成前两层）

定位是「组织级基础设施」，这一层要分几步走。已完成：**Agent 成为一等公民**
（身份 / 分工 / 治理）、**按 Agent 的运算基准**（用量 + 治理信号）、**趋势对比**
（等长上一窗口 + 分格走势），细节见 [`AGENTS.md`](AGENTS.md)。下面几项**都是有意不做**，
每条都写了开工条件。

**前三项的规则已经定稿成设计稿**（只有设计，没有代码）：
[`A2A.md`](A2A.md)、[`AGENT_QUOTA.md`](AGENT_QUOTA.md)、[`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md)。
评审通过后按各文档的「分期」开工，不要绕过文档直接写实现——三份稿子里被推翻的直觉做法都有记录。

> 2026-11-11：构建助手 **P0 已上线**（`studio` 内置 Agent + 4 个工具 + 悬浮面板 + 内联确认，
> 迁移 `e1c7a4f92b36`）。上线过程三次冒烟暴露的根因已归入 [`LESSONS.md`](LESSONS.md) §2.5。
> 剩下的是 P1（技能 / 工具的四条写工具）与 P2（平台用法问答）。

| 事项 | 做法（若开工） | 为什么现在不做 | 开工条件 |
|---|---|---|---|
| ~~**Agent 之间委派（A2A handoff）**~~ | **P0–P3 全部实现**（2026-11-12）：`delegate_to_agent` + `agent_subset` + 深度/环/预算 + `kind=agent` span + `agent_delegated` 审计 + 能力目录接口；P1 补齐 `refs`（artifact / message / knowledge / entity 四类 + 三态拒绝 + 材料与附件转交 + 无证据不过桥，见 `app/services/reference_service.py`）；P2 解除子调用只读，审批单冻结整条 `delegation_chain` 并在执行期逐跳复核（`DELEGATION_REVOKED`），结论经 `parent_thread_id` 写回发起会话。规则与偏差见 [`A2A.md`](A2A.md)、[`AGENT_CONNECT.md`](AGENT_CONNECT.md) | — | **P3 全部完成**（2026-11-12）：P3-a 委派预算按 Agent 可配、P3-b `delegated_turns`、P3-c 产物按会话保留（`save_artifact` + 引用按会话收敛）。跨会话取用产物仍是独立产品决策 |
| **Agent 级配额 / 成本** | 原设想是 `tenant_quota` 同级加 `agent_quota` 表；设计稿改为**在 `agents` 表加四列**（独立表会留孤儿行、且读路径在每次工具调用上）。见 [`AGENT_QUOTA.md`](AGENT_QUOTA.md) | 现在配额在租户粒度；按 Agent 拆需要先看到真实调用分布，否则阈值是拍的 | `GET /observability/agents` 攒够 2 周数据，能看到 Agent 之间的量级差异 |
| **Agent Studio 助手（构建助手）** | 内置 Agent `studio` + 13 个 `studio_*` 工具 + 悬浮面板；改删走内联二次确认。见 [`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md) | P0 与 P1 已完成上线；余下按 P2 推进 | P2：平台用法问答（不进租户知识库） |
| ~~**Agent 绩效看板**~~ | 已完成：`/observability/agents` + 控制台「按智能体」表（轮次 / 错误率 / p95 / token / 成本 / 被拒 / 驳回 / 走势 / 较上一窗口） | — | — |
| **Agent 绑定模型** | `model_provider` 上加一份可被 Agent 引用的配置，带降级链 | 全局一个模型配置最简单也最可预测；按 Agent 选模型要先有成本分摊 | 有「某类任务必须用更强模型」的具体诉求，且 `/agents` 的成本列已经在用 |
| **Agent 下发到多副本** | 边界随 state 走，已天然多副本安全；剩下的是缓存一致性 | 单机部署下没有这个问题 | 上多实例 |

判断依据统一是「有没有真实需求 + 前置数据够不够」，不是时间表。

## 4.6 连接器的四件没做的事

`docs/CONNECTORS.md` §11 列了全部欠账，这里只收「要开工时该知道代价」的四件。
共同前提：**目前所有外部系统都只有一条服务端配置 + 每个租户一个令牌**，
没有出现「一个人一个账号」的真实诉求。

| 事项 | 做法（若开工） | 为什么现在不做 | 开工条件 |
|---|---|---|---|
| **OAuth2 / 每用户授权** | 加 `connector_oauth_apps`（客户端凭据）与 `connector_user_tokens`（按用户绑定的 access/refresh），回调路由 + 刷新任务；`auth_type` 加 `oauth2` | 授权码流程要浏览器回调、令牌刷新、按人绑定，和「一条服务端配置」不是同一类东西。硬塞会得到一个「看起来支持 OAuth、实际要人工每月换 token」的假能力 | 出现「必须按人区分外部账号」的系统（如某个 SaaS 的权限完全按登录人走），且有真实用户要接 |
| **连接器级配额与限流** | 在 `connectors` 上配额，或在限流器里加 `(tenant, connector)` 维度 | 现在只有平台级与工具级限流。一个租户打满一个连接器会影响同连接器上的其他租户，但当前外部依赖只有一个、调用量很小 | 同一个连接器上出现两个以上租户，且其中一方明显占用更多 |
| **端点连通性巡检** | 维护循环里按 `health_check_interval` 逐个 `check`，落 `health_status` | 现在只能手动检查，所以界面上的健康状态可能是很久以前测的（有时间戳）。巡检会给对端增加稳定负载，且失败了没人处理 | 有连接器进入「必须一直可用」的等级，或出现过「页面上写着连通、实际早已不可用」 |
| **凭据加密存储** | 三处一起改：`connector_credentials.secret`、`mcp_registered_tools.token`、`model_providers.api_key`；用 KMS 或 `pgcrypto` + 环境密钥 | 现状是库里明文，靠数据库与备份的访问控制。只加密一处比不加密更糟——会让人以为整体都加密了 | 有合规要求（等保、审计）或数据库不再由自己独占 |

## 5. 运维建议（等确认）

| 事项 | 理由 |
|---|---|
| 两个 systemd 服务加 `MemoryMax` / `CPUQuota` | 单机跑 API + Studio，一个进程吃满内存会把另一个拖死；限流只管请求速率，管不了内存 |

## 6. 长期未实现（README「已知边界」对应项）

- Checkpointer 换 PostgresSaver（多实例部署时才需要）
- Langfuse 上报（观测已自建，除非要外部分享 trace）
- RBAC 管理接口（现在按角色授予 scope，靠迁移脚本改）

## 7. 长对话上下文（V1–V3 已上线）

设计见 [`CONTEXT.md`](CONTEXT.md)，观察期读法见该文 §8.1。

**V1 + V2 已于 2026-11-13 上线**（`dd04e83`…`41ca15f`）：历史按轮注入、压缩落库、
五个降级分支、`conversations.last_prompt_tokens` 回写。线上已真实压缩两次
（2,421→236、3,228→272，gain 0.90/0.92）。**V3 呈现同日晚落地**：trace 详情一行
上下文 chip（点击展开摘要全文）、会话页在压缩水位插分隔条，接口增返 `summaries[]`。

| 阶段 | 做什么 | 状态 / 开工条件 |
|---|---|---|
| V1 | measure + inject | 已完成 |
| V2 | 压缩 + 落库 + 五条降级 | 已完成 |
| V3 | 控制台两处呈现：trace 概要卡下一行 chip；会话页在压缩边界插分隔条 | 已完成。chip 的数字全部来自 span 实测（不是另算一遍），改阈值不需要改文案 |

一件**不在本文件**的遗留：

- **观察期调参**（不改代码）：看 `context.compact` 的**频次**与 **gain**，不看 ratio。
  现象与参数的判读表在 `CONTEXT.md` §8.1。要调频率动 `KEEP_TOKENS` /
  `MIN_EVICT_TOKENS`，**不是**动窗口。

~~供应商级 `context_window`~~ **已于 2026-11-15 接上**：`ModelEntry.context_window` +
`resolve_text_model()` 解析 + 控制台每个文字模型一行输入框，`window_source`
（provider / env）写进 `context.measure` span。细节见 `CONTEXT.md` §3.2。

与观测 M4 的关系：`conversations.last_prompt_tokens` 这一列同时是成本统计的数据源，
两件事可以一起做，但**不要把成本统计塞进压缩逻辑**——统计要的是历史，压缩要的是当下。

## 8. 已修复：审批到达终态后运行不收口

2026-11-13 修复。修复前 `app/services/approval_service.py` 从头到尾没写过
`RunStatus`，全仓只有 `nodes.py:1499`、`chat_service.py:495/637` 三处置
`WAITING_FOR_APPROVAL`，**没有任何回收路径**——审批单一过期，那一轮运行就永久挂着。
线上积了 5 条（11-12 06:56–07:02，审批单分别已 VERIFIED / FAILED×2 / EXPIRED×2）。

**影响要说准**：现有接口没有按 `agent_runs.status` 呈现或统计的地方
（`/observability/agents` 读的是 `obs_traces`），所以**界面上看不到**。真实代价是
数据不自洽：状态停在「等审批」，无法区分「还在等人点」与「人早点了、只是没人收口」。
将来做在飞运行监控、告警或清理时，这些行会被当成还在等人确认。

`finished_at` **不是空的**——`finish_run` 无条件写它，所以停在等待态的行反倒有结束时间。
我最初把影响说成「看板挂 5 个点不动的待办、污染 p95、finished_at 为空」，三条都不成立，
都是没核对读取方就下的结论。已在 `LESSONS.md` §2.2 留了这条教训。

**修的时候发现了第二层**：`persist_state` 只判断 `execution_status == "PENDING"`，
其余一律 `COMPLETED`。于是**工具执行失败、用户取消、审批过期**这几种情形全部被记成
「完成」——而且是朝好看的方向错，失败率与 p95 都不会报警。所以除了收口，
还必须让终态推导认审批的结论（`nodes.run_status_for`），否则收口写进去的
`CANCELLED` / `FAILED` 会被下一次 `finish_run` 覆盖回 `COMPLETED`。

映射（`run_service.close_runs_for_approval` 与 `nodes.run_status_for` 两侧一致）：
VERIFIED/EXECUTED → COMPLETED；FAILED/EXECUTION_UNKNOWN/RECOVERY_REQUIRED → FAILED；
REJECTED/EXPIRED → CANCELLED。取消分支同时带 `error_category=approval_rejected`，
否则 `persist_state` 会用 state 里的空值把审批侧写好的原因抹掉。

修法（`run_service.close_runs_for_approval`）：审批落终态时按
`agent_runs.approval_id` 与 `approval_requests.run_id` 两条关联收口，
`UPDATE ... WHERE status = WAITING_FOR_APPROVAL` 保证幂等与不覆盖；
映射为 VERIFIED/EXECUTED → COMPLETED、FAILED/EXECUTION_UNKNOWN/RECOVERY_REQUIRED
→ FAILED、REJECTED/EXPIRED → CANCELLED。五个终态写入点全部接上，
另加 `reconcile_waiting_runs` 兜底对账（维护循环每 30 秒一次，10 分钟宽限），
历史脏行由它自动收拾。

测试 `tests/integration/test_approval_run_closure.py` 11 条，含两条反向验证
（去掉等待态守卫、去掉过期收口，各自让对应断言变红）。
