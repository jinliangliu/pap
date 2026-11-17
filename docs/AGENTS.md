# 智能体（Agents）

平台的定位是：把「多个聪明 Agent 的偶然协作」，变成「有身份、有分工、有治理、可沉淀、
可持续运营的组织级基础设施」。

本文回答两件事：这个定位在代码里落到哪、以及还差什么。**先说结论**：本轮之前，
平台有很强的治理与沉淀能力（审批、审计、租户隔离、知识库、记忆、观测），
但**没有「智能体」这个对象**——只有一个无名的执行图。「多智能体」不是优化问题，
是从 0 到 1 的问题。所以本轮只做一件事：让 Agent 成为一等公民。

---

## 1. 五个维度的现状（诚实版）

| 维度 | 本轮之前已有 | 缺的是什么 | 本轮补了什么 |
|---|---|---|---|
| **身份** | User / Role / Permission / Tenant；可信头解析；ORM + RLS 双层隔离 | 只有「人」的身份。Agent 无名、无归属、审计里只有 `user_id` | `agents` 表（租户内唯一 `name`）、生命周期、`revision`；`obs_traces.agent_name` |
| **分工** | Skill（作业指令 + 触发词 + 工具白名单）、Tool registry | Skill 回答「这类活怎么做」，不回答「**谁**负责什么、能碰什么」 | `allowed_tools` / `allowed_skills` / `can_write` 三层职责边界 |
| **治理** | 写操作强制审批、追加式审计、租户工具策略、配额、站点授权 | 治理对象是「一次工具调用」，不是「一个执行主体」。没有 agent 级停用 | `status`（draft/active/disabled）；审批时冻结边界快照，执行前复核 |
| **沉淀** | 知识库、长期记忆、技能、观测 trace | 观测里没有 agent 维度，答不出「哪个 Agent 值得留」 | `agent_name` 成列（可索引），不再只塞在 `attrs` JSON 里 |
| **运营** | 监控、部署审计、检索评测 | 没有 agent 清单：谁在用、谁闲置、加一个要改代码 | 控制台「智能体」页：清单、边界摘要、新建/编辑/启停 |

一句话概括本轮的范围：**先把「谁在做事」立起来**。其余四层要么已经很强，
要么必须等这一层存在之后才有意义（没有 agent，谈 agent 绩效是空转）。

---

## 2. 为什么 Agent 必须是「数据」，不是「代码」

有两种做法都能叫「多智能体平台」：

- **Agent = 代码对象**：开发者在图里加节点、写类。灵活，但每加一个角色都要发版，
  业务侧无法自助，停用一个角色也要发版。本质是**开发框架**。
- **Agent = 配置对象**：有名字、有职责、有边界、有生命周期，在控制台上增删改。
  本质是**组织级基础设施**。

平台选后者。判据不是「哪种更优雅」，而是「谁来做这件事」：
组织里定义「客服 Agent 只能读工单不能退款」的人是业务负责人，不是发版的人。
一个需要工程师陪同才能调整职责边界的平台，不叫治理。

---

## 3. 三条不变量（改这个模块前先读）

这三条写在 `app/services/agent_service.py` 的模块 docstring 里，
实现落在 `app/tools/gateway.py`。它们不是「设计建议」，是不变量——
违反任意一条，Agent 就退化成「多开几个提示词」。

### 3.1 只收窄，不放宽

```
有效权限 = 当前身份的权限（scope / 角色 / 站点授权） ∩ Agent 边界
```

Agent 永远不能让人看到原本看不到的工具，也不能让人凭它拿到新的 scope。
`allowed_tools` 只是**从「你已经能用的工具」里再减一部分**。

`allowed_connectors` 是同一件事的粗粒度版本：接一个外部系统时不用再列二十个
工具名，写一个连接器名即可。两者是并集，且**只配连接器时内置工具会消失**——
人明确表达了「只给这些」。这条与「收窄」并不矛盾，但它是本模块里最容易
凭直觉写错的一行，细节见 `docs/CONNECTORS.md` §5。

### 3.2 `can_write` 是更严的开关，不是审批豁免

关闭后，写类工具（`WRITE` / `EXECUTE` / `DESTRUCTIVE` / `EXTERNAL`）连目录都不出现；
打开也只是解除这一层，写操作照样走审批。

### 3.3 不传 Agent 等于平台基线

`BASELINE` 边界是 `allowed_tools=None`、`can_write=True`、`instruction=""`，
与没有这套机制时**逐字等价**。升级不会改变任何既有会话的行为——
这条是兼容性红线，也是「默认 Agent 刻意不预填职责说明」的原因：
它的语义就是「不做额外约束的基线」。填了就会进提示词，那是显式编辑的结果。

---

## 4. 数据模型

`app/models/agent.py`，表 `agents`（迁移 `a2f8c4d60b73`）。

| 字段 | 说明 |
|---|---|
| `tenant_id` | 继承 `TenantScoped`：ORM 自动过滤 + RLS 兜底 |
| `name` | 租户内唯一，对话时用它指定。**创建后不可改**（改名会破坏已接入的客户端） |
| `display_name` | 给人看的名字，可以随时改 |
| `description` | 职责说明（≤512 字），会进提示词 |
| `instruction` | 补充作业约束（≤4000 字），会进提示词 |
| `allowed_tools` | `null` = 不额外收窄；`[]` = 一个都不给；数组 = 只允许这些 |
| `allowed_connectors` | 与 `allowed_tools` **并集**：连接器里的工具整体可见。两者都为 `null` 才是不额外收窄 |
| `allowed_skills` | 同上，只是少给几条作业指令 |
| `can_write` | 默认 `false`。新建 Agent 默认只读 |
| `max_delegation_depth` | 委派深度上限（0–5）。`null` = 平台默认 2；`0` = 不转交 |
| `delegation_budget` | 单轮委派次数（0–10）。`null` = 平台默认 3；`0` = 本轮不转交 |
| `status` | `draft` / `active` / `disabled`，默认 `draft` |
| `is_default` | 每租户一个，部分唯一索引兜住 |
| `revision` | 边界每变一次 +1，审批与观测记录它 |
| `created_by` / `updated_by` | 谁定义的 |

三个刻意的设计：

- **委派预算的 `null` 与 `0` 也必须分开。** `null` 是「走平台默认」，
  `0` 是「一次都不许」；用 `x or DEFAULT` 折配置会让后者静默变成前者，
  而界面上明明写着 0。
- **`null` 与 `[]` 必须分开。** 前者是不收窄，后者是一个都不给。混用会让
  「忘了填」和「明确禁止」在库里长得一样，事后谁也说不清当时想表达哪个。
- **`is_default` 用部分唯一索引，不靠应用层自觉。** 不传 agent 的请求全落到默认
  Agent 上，出现两个就意味着「不传时行为不确定」。这类约束交给数据库。
- **`revision` 只在实际变化时递增。** 界面上反复点保存不该让版本号虚增，
  否则「第 7 版边界」这种指认就失去意义。

---

## 5. 一次对话里边界怎么生效

边界不是「页面上的一行字」，它在两个地方各拦一次：

```
POST /chat { agent: "ops" }
  → 路由层解析（app/api/routes/chat.py）
      agent_service.resolve_for_chat：不存在 404、草稿/停用 409
      （不静默回落到默认——「我选了它，它却换了一个人回答」比报错更难排查）
  → 摊平进 LangGraph state（只放执行图要用的字段，不存整段提示词）
  → nodes.plan_request
      ① ToolGateway 目录过滤：模型看不到越界工具
      ② 职责块拼在 system 末尾（顺序：基础规则 → 角色职责 → 作业步骤）
      ③ 技能被边界排除时当作没命中：不注入指令、不记 skill_hits
  → nodes._context → ToolExecutionContext 携带边界
  → ToolGateway._check_permissions 执行前再拦一次
```

**为什么要在网关里再拦一次，而不是只在目录里过滤？**
模型可能凭记忆拼出目录里没有的工具名。只在目录层过滤，等于「看不见但调得动」。
`agent_denial()` 把判断抽成一份，目录过滤与执行拦截共用，避免两处各写一遍出现缝。

观测上：`trace_run(agent_name=...)` 在 trace 创建时就带上，不等后续补。

**列写进去就必须能读出来。** `GET /api/v1/observability/traces` 返回 `agent_name`
并支持 `?agent=<name>` 过滤，控制台的链路列表也新增「智能体」列——只写不读
等于没沉淀，后面想按 Agent 看绩效还得再补一次接口。存量行的 `agent_name` 是
`NULL`（那时还没有这个概念），接口原样返回 null、按具体名字筛时不计入，
不回溯编造。

### 5.1 被拒绝的调用要记账，且要说清是谁拒的

拒绝有四类来源，治理含义完全不同：

| `reason` | 谁拦的 | 该做什么 |
|---|---|---|
| `scope` | 这个人缺 scope | 改授权 |
| `role` | 这个人所在角色不被允许 | 改角色 |
| `tenant` | 整个组织没向该角色开放该工具 | 改租户策略 |
| `agent` | 人有权限，但当前智能体边界没放开 | **改 Agent 配置**（多半是配窄了） |

以前这四类只有一句文案、都归成 `tool_rejected`，而 `TOOL_DENIED` 这个审计事件
**定义了却从来没被写过**（审计只在工具真正执行后落库，拒绝发生在执行之前）。
结果是「这个 Agent 的边界被撞了几次」在系统里根本无解。

现在：

- `ToolPermissionDeniedError.reason` 携带来源，抛之前先写一条 `tool_denied` 审计
  （含 `denial_reason` / `agent_name` / `agent_revision`，**不记参数值**——
  拒绝发生在参数校验之前，那时传入的内容可能是模型瞎拼的）；
- `DbAuditSink` 不再把 `tool_denied` 按 `success=False` 改写成 `tool_failed`：
  前者是治理信号，后者是故障信号，混在一起就再也数不开了；
- 观测口径细分：`tool_blocked_by_agent` / `_role` / `_tenant` / `_scope`，
  通用失败仍是 `tool_rejected`。

**顺带改了一个签名**：`prepare_call` 由同步改为异步。拒绝的记账必须 await，
同步方法里做不到；它只有一个调用方（`create_and_wait_approval`），代价很小。

### 5.2 更常见的情况：边界让工具「消失」，而不是「被拒」

上面那套记账只兜住一类少见情形——模型凭记忆拼出目录里没有的工具名。**日常更常见的是
工具压根没进目录**：模型看不到它，只会换个说法说「我这边做不了」，于是没有异常、
没有拒绝、没有任何记录。「这个 Agent 的边界配窄了」这个配置问题，反而毫无信号。

所以 `plan_request` 里额外算一次差值：拿「不设边界的目录」减「设了边界的目录」，
把结果写进 trace 的 `agent_hidden_tools` 属性，并记一条
`event=agent_hidden_tools` 的日志。基线上下文（`allowed_tools=None` 且
`can_write=True`）直接早退，不额外扫目录。

这一步的定位是**诊断**，不是拦截：它不改任何行为，只回答「这个 Agent 少了哪些工具」。

### 5.3 运算基准：按 Agent 看用量与治理信号

`GET /api/v1/observability/agents` 把「谁在被用、谁在出错、被挡了几次、花了多少」
压到一行里，控制台观测页有一张对应的表（点一行即筛出它的调用链）。默认窗口
168 小时：一天的样本量不足以判断「这个 Agent 是不是只有一个人在试」。

**统计口径分几处取数，因为每种数各有自己的分母**：

| 数 | 来源 | 为什么不能从 trace 推 |
|---|---|---|
| 轮次 / 错误率 / 耗时 / token / 成本 | `obs_traces` | — |
| 被拒次数 | `audit_logs` 的 `tool_denied` | 拒绝发生在执行**之前**，那一轮可能正常回答了，trace 上看不出它被挡过 |
| 审批 / 驳回 | `approval_requests` | 审批是跨请求的，可能跨越窗口边界 |
| 被委派次数 `delegated_turns` | `obs_spans` 的 `kind=agent` | 它**在** trace 里，但不是这一行自己的轮次：委派的子调用跑在发起方那条 trace 上 |

四条口径约定：

1. **名字是快照，不回溯改写。** Agent 删除后，历史行照样按当时那个名字统计，
   `status` 返回 `null` 表示「库里已经没有它了」，而不是报错或归到别人头上。
2. **存量行单独报。** `{agents 之和} + unattributed_traces == total_traces` 恒成立；
   把 NULL 塞进某个 Agent 就等于编造归属。概览的 `by_agent` 则把 NULL 记作 `—`，
   让分布图各格之和等于总数。
3. **成本没报就是 `null`，不是 `0`。** 0 会被读成「这个 Agent 免费」。
4. **`delegated_turns` 与 `traces` 不能相加。** 前者是「被叫去做事的次数」，
   后者是「发起的轮次」；分母不同，界面上分列，不做合计行。一个 Agent 可以
   0 轮自己的对话却被叫了 20 次——而这正是「谁在被使唤」要回答的那个人，
   所以它也要进名字并集，不能因为「没有轮次」就被漏掉。

**趋势是第二层：截面只能回答「现在多大」。** 同一接口再给一条分格走势和一个等长的
上一窗口，用来回答「在不在长」。这段数据有四条约束，缺一条就会得出假结论：

| 约束 | 做法 | 不这样会怎样 |
|---|---|---|
| 滚动窗口而非自然周 | 两段都止于此刻、等长 | 按自然周切，「周一上午」永远显得暴跌 |
| 从末尾往前切格 | 最后一格收在「现在」；只有最旧一格可能 `partial` | 按整点对齐，最新一格是半格，看起来像下滑 |
| 算不出比例就不算 | `change=null` + `change_reason`（`no_baseline` / `created_after_baseline_start` / `low_sample`） | 把「没法比」画成「持平」或「+100%」 |
| 只比轮次 | token / 成本有缺报，不参与比例 | 拿有缺口的量算比例，凭空多出涨幅 |

`low_sample` 的门槛是任一侧不足 5 轮——这不是显著性检验，只是拦住最离谱的读数
（2 轮 → 3 轮是「涨 50%」，实际是同一个人的两次重试）。`created_after_baseline_start` 比
`no_baseline` 更靠前判断：新建的 Agent 上一窗口通常是 0，只说「没基线」就分不清
它是刚起步还是被放弃了。

---

## 6. 审批：跨请求的边界快照

审批是唯一跨两次 HTTP 请求的路径，也是「边界治理」最容易漏的地方。

审批单现在保存 `agent_id` / `agent_name` / `agent_revision` / `agent_allowed_tools` /
`agent_can_write`（迁移 `c4a9f2e75d16`）。执行时取**快照 ∩ 当前配置**：

| 情形 | 结果 | 为什么 |
|---|---|---|
| 期间把工具从 Agent 的允许清单里删掉 | **立即拦住**，审批单以 `AGENT_BOUNDARY_CHANGED` 拒绝 | 收窄要立刻生效，否则「已批准的写操作」成了绕过新边界的后门 |
| 期间把只读 Agent 放开成可写 | **不生效**，仍按只读执行 | 一张按只读批准的写单，不该因为事后放开而照跑 |
| Agent 已停用 / 已删除 | 拒绝，`AGENT_UNAVAILABLE` | 停用的语义是「它不能再做事」，包括已经排队的 |
| 这张单子是被委派来的，期间**上游**某一跳被收窄 | 拒绝，`DELEGATION_REVOKED` | 执行方可能一点没变，变的是把它叫来的那一跳。与 `AGENT_UNAVAILABLE` 分开：否则排障会去查一个没有问题的 Agent |

委派单还多存一列 `delegation_chain`（根 → 直接上级），复核从直接上级往根逐跳做；
只比直接上级会在 A→B→C 里漏掉「A 收窄、B 未变」。规则见 [`A2A.md`](A2A.md) §9.1。

只存 `agent_id` 做不到这一点：执行时只能看到「现在」的边界，
上面第二行就会失守。

---

## 7. 接口与权限

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/agents` | 清单（所有成员可读）；首次访问惰性建出默认 Agent |
| POST | `/api/v1/agents` | 新建，需 `agent:write`；默认建为**草稿 + 只读** |
| GET | `/api/v1/agents/{ref}` | 按 `name` 或 id 取详情 |
| PATCH | `/api/v1/agents/{ref}` | 改职责与边界；显式传 `null` 表示取消收窄 |
| POST | `/api/v1/agents/{ref}/status` | `draft` / `active` / `disabled` |
| DELETE | `/api/v1/agents/{ref}` | 只允许删**已停用**的非默认 Agent |

读开放给所有成员的理由：不知道有哪些 Agent，就没法在对话时选。
写需要 `agent:write`：定义「谁来做事、能碰什么」是治理动作，不是日常操作。

审计事件新增四类，可以按类型筛：`agent_created` / `agent_updated` /
`agent_status_changed` / `agent_deleted`。`agent_updated` 只记边界变化前后，
不记整段指令正文（可能很长，也可能含业务细节）。

`PATCH` 的 `allowed_tools` / `allowed_skills` 用 pydantic 的 `model_fields_set`
区分「没传」与「显式传 null」——前者保持原值，后者是明确的放宽动作。
用 `is not None` 判断会把两者混成一个，这是这个接口最容易写错的一行。

---

## 8. 暂不做（附触发条件）

下表中前三项的**规则已经定稿成设计稿**，实现前先读对应文档；稿子里记录了
被推翻的直觉做法（尤其是 A2A 的子集校验为什么不能放在配置期、Agent 配额为什么不能建独立表）：
[`A2A.md`](A2A.md)、[`AGENT_QUOTA.md`](AGENT_QUOTA.md)、[`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md)。

| 事项 | 为什么现在不做 | 开工条件 |
|---|---|---|
| ~~**Agent 之间委派（A2A handoff）**~~ | **P0 / P1 / P2 已上线**：`delegate_to_agent`、`agent_subset` 唯一判定、深度 2 / 环 / 单轮 3 次、`kind=agent` span、`agent_delegated` 审计、`GET /agents/{ref}/capabilities`；P1 的 `refs` 引用传递；P2 解除子调用只读 + 审批单冻结 `delegation_chain` 并逐跳复核。规则见 [`A2A.md`](A2A.md)，数据面与偏差见 [`AGENT_CONNECT.md`](AGENT_CONNECT.md) | — | 余下 P3：委派预算按 Agent 可配、`delegated_turns` 指标、跨会话共享产物 |
| **Agent 级配额 / 成本上限** | 现有配额在租户粒度。按 Agent 拆需要先有稳定的调用量分布，否则阈值是拍的。方案见 [`AGENT_QUOTA.md`](AGENT_QUOTA.md)（加列，不建表） | `obs_traces.agent_name` 攒够 2 周，能看到 Agent 之间的成本差异 |
| **Agent 绩效看板**（成功率 / 驳回率 / 时延） | 依赖上一行；且「主动调用 → 被驳回」目前只记在审批表，口径要先定 | 有 ≥2 个 Agent 在真实使用 |
| **多 Agent 并发 / 辩论** | 这是执行图的形态变化，不是配置问题；单机跑并行 agent 会先把时延打爆 | 单 Agent 的成功率与边界稳定性有数据支撑之后 |
| **Agent 绑定模型** | 现在全局一个模型配置。按 Agent 选模型要先有成本分摊与降级策略 | 有「某类任务必须用更强模型」的具体诉求，且能接受多份凭据 |

判断依据统一是「有没有真实需求 + 前置数据够不够」，不是时间表。

---

## 9. 已知边界

- **Agent 是租户级，不按用户隔离。** 同一租户里任何成员都能选用已启用的 Agent。
  这是「组织基础设施」的应有之义——但意味着**不要**把个人偏好写进 Agent 的职责说明。
- **边界快照只在审批路径生效。** 一次对话进行中改边界，本轮仍按进入时的快照执行
  （否则同一轮内前后两个工具可能适用两套规则，出了问题没人复现得出来）。
- **默认 Agent 不能停用、不能删除。** 停用等于让所有不传 agent 的请求 400；
  要停就停整个租户的能力，而不是留一个隐形的坑。
- **删除要求先停用。** 停用已覆盖「先别用」，直接删一个还在跑的 Agent 会让历史审计里的
  `agent_id` 指向不存在的对象。
- **Agent 不改变模型的「知识」。** 它约束的是工具与职责口吻，
  知识范围仍由知识库的 `access_scope` 决定，不是 Agent 的字段。
- **内置 Agent 的边界是固定的。** 构建助手（`studio`，`is_builtin=true`）能改别人的配置，
  但它自己的 `allowed_tools` / `allowed_skills` / `can_write` / `status` 都不可改、不可停用、
  不可删除（`agent_service.update_agent` / `set_status` / `delete_agent` 里各挡一次）。
  允许改它等于给了一条自提权路径：先给自己加一个写工具，下一轮就能用，
  而中间没有任何人批准过这个新增能力。职责说明仍可编辑——那是语气偏好。
  见 [`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md) §3.1。
- **`status` 落库是小写 value，不是成员名。** 迁移建的 PG 原生枚举
  `agent_status` 标签是 `draft/active/disabled`，而 SQLAlchemy 默认按成员名
  （`ACTIVE`）绑定，首次插入就会 500。模型里必须带
  `values_callable=lambda e: [m.value for m in e]`——和 `tenant.status` 同一个坑。
  回归用例读裸 SQL 断言，ORM 属性看不出差别。

---

## 10. 相关文件

| 位置 | 内容 |
|---|---|
| `app/models/agent.py` | 模型与生命周期枚举 |
| `app/services/agent_service.py` | 三条不变量、默认 Agent、边界解析、CRUD |
| `app/tools/gateway.py` | `agent_denial()`：目录过滤与执行拦截共用的唯一判断 |
| `app/tools/builtins/studio_tools.py` | 构建助手的平台管理工具（见 [`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md)） |
| `app/agents/nodes.py` | 职责块注入、技能边界过滤、上下文携带边界 |
| `app/services/approval_service.py` | `_execution_agent_boundary()`：快照 ∩ 当前 |
| `app/api/routes/agents.py` | 管理接口 |
| `static/console/assets/console.js` | 「智能体」页与对话台选择器 |
| `tests/unit/test_agent_service.py` | 边界、生命周期、网关拦截、审批交集 |
| `tests/integration/test_agents.py` | 权限、CRUD、对话 404/409、边界真的能拦住工具 |
