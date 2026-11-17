# Agent 级配额

> 本文是设计稿，不是现状说明。当前代码里**没有** Agent 级配额，只有租户与用户两级
> （`app/services/rate_limit_service.py`）。落地前请按第 10 节分期推进，不要一次全上。

## 0. 一句话

Agent 配额是**租户配额的切片**，不是一层新预算。它只能让某个 Agent 更早停手，
不能让任何 Agent 获得比租户上限更多的调用机会。

## 1. 缺口：一个 Agent 能拖垮同租户的其它 Agent

现有三处闸门，一个都不认识 Agent：

| 闸门 | 键 | 粒度 |
|---|---|---|
| 用户 × 工具 × 分钟 | `u:{tenant}:{user}:{tool}:{bucket}` | 防止单个人刷爆 |
| 租户 × 分钟 | `t:{tenant}:tool:{bucket}` | 防止整个组织刷爆 |
| 租户 × 资源 × 日 | `quota:t:{tenant}:{resource}:{day}` | 每日对话数等 |

问题不是「不够严」，而是**粒度错了**。Agent 的定位是独立执行主体（`docs/AGENTS.md` §1），
一个主体的失控应当只让它自己停摆。现在它会把同租户所有 Agent 一起拖下水：
某个 Agent 进入重试循环，把 `t:{tenant}:tool` 打满，其它 Agent 的正常调用全部被拒，
而首恶在观测上只是「调用量大」，看不出是它把别人挤掉的。

## 2. 三条不变量

### 2.1 只能更严，不能更宽

**不做配额分配，做闸门叠加。**

两种做法都叫「Agent 级配额」，含义完全不同：

| 做法 | 语义 | 加一个 Agent 要做什么 |
|---|---|---|
| 分配式 | 租户 600/分钟，切给 A 200、B 200，余 200 给无名请求 | 必须重新分配，否则新 Agent 无额度 |
| 叠加式 | 每个 Agent 可另设上限，但总和仍受租户 600 限制 | 什么都不用做，不配就不限 |

选叠加式。理由不是「更优雅」，而是**默认值必须让行为逐字不变**：`None` 时这段代码
等于不存在，与三条不变量里的「不传 Agent 等于平台基线」同一条红线。分配式做不到这一点——
它一上线就要给每个租户切蛋糕，而切蛋糕的默认值是拍出来的。

叠加式的代价要认：三个 Agent 各设 200/分钟，仍然可以把租户的 600 打满。
这是对的——租户闸门本来就是最外层兜底，Agent 闸门管的是「谁先停」，不是「总量多少」。

### 2.2 `None` 与 `0` 必须分开

这是本次最容易写错的一行。

同一个 `0`，在这套代码里现在有两种读法：

- `app/models/tenant.py` 的 `TenantQuota`：「值为 0 表示**沿用平台默认上限**」；
- `app/services/rate_limit_service.py` 的 `enforce_daily_quota`：`if limit <= 0: return`，
  即 **0 表示不限**。

前者的 0 已被 `resolved_*()` 解析成默认值，所以 0 不会真的传到后者。
两处各自自洽，但拼在一起就变成第三个隐含语义。Agent 配额**不复用这套解析**：

| 值 | 语义 | 谁会用 |
|---|---|---|
| `None` | 不额外限制，只受租户与用户闸门约束 | 绝大多数存量 Agent |
| `0` | 完全禁止（这个 Agent 一次也不许调） | 下架中的 Agent、纯演示角色 |
| `> 0` | 该 Agent 自己的上限 | 需要隔离噪声的 Agent |

实现上必须把 `is None` 与 `<= 0` 分开写，并且**给 0 单独一个用例**：
`0` 要能拒掉第一次调用，`None` 要放行到租户上限为止。用 `<= 0` 兜底会把这两者
合并成「都不限」，而这个错误在测试里如果只测 `None`，是看不出来的。

### 2.3 默认 Agent 不设额度

不传 `agent` 的请求全部落到默认 Agent（`is_default`，部分唯一索引兜住）。给它设额度，
等于给整个租户的「无名流量」加了一个隐形上限，而这个上限不会出现在任何租户配置页上。

所以：新建 Agent 时四个配额字段全部 `NULL`；控制台不对默认 Agent 推荐任何数值；
`PATCH` 允许给默认 Agent 设，但要在响应里明确「这会影响所有未指定 Agent 的请求」。

## 3. 计量维度：两类，性质完全不同

| 维度 | 何时判 | 能否事前知道 | 超限后果 |
|---|---|---|---|
| 调用速率（次/分钟） | 请求前 | 能 | 拒这一次工具调用 |
| 每日轮次 | 请求前 | 能 | 拒整轮对话 |
| 每日 token | 请求后结算 | **不能** | 下一次请求被拒 |
| 每日成本 | 请求后结算 | **不能** | 下一次请求被拒 |

**为什么 token 与成本必须事后结算。** 一次对话要调几次工具、每次带多少上下文，
在开始前不可知——这是模型自己决定的。请求前只能读「已用量」再判断是否已经超限，
所以最后一轮可能超支，超支幅度约等于单次请求的成本。

这是有意接受的软上限，不是实现缺陷。但必须写在产品文案里：**到上限不是立刻停，
是下一次停**。反过来说，任何声称「到上限一分不多花」的实现都是错的，
除非改成预扣（见第 12 节）。

## 4. Redis 键结构

```
rate-limit:t:{tenant}:a:{agent}:{bucket}
quota:t:{tenant}:a:{agent}:turn:{day}
quota:t:{tenant}:a:{agent}:token:{day}
quota:t:{tenant}:a:{agent}:cost:{day}
```

`agent` 段用 **name 而不是 id**：name 租户内唯一、创建后不可改（`docs/AGENTS.md` §4），
在 Redis 里直接可读，排障时不用再 join 一次。代价见第 12 节第一条。

`{bucket}` 与 `{day}` 沿用现有 `_bucket()` / `_day_bucket()`，不新造时间语义：
两套时间窗并存的系统，一定会出现「分钟闸门放行、日闸门拒绝」这种解释不清的组合。

## 5. 计数顺序与原子性

### 5.1 必须 Agent 先判，租户后判

```
① 读 agent 计数 → 已超 → 直接拒绝，**不**动租户计数
② 未超 → INCR agent 计数
③ INCR 租户计数 → 已超 → 拒绝
```

反过来（先租户后 agent）的后果：Agent 被拒时租户额度已经被消耗。多个受限 Agent 的
拒绝会累积成租户级的额度流失，最后连不设额度的 Agent 也被拖停——正是本次要修的病。

步骤 ① 与 ② 之间不能有 `await` 之外的空隙，否则两个并发请求会同时读到「未超」。

### 5.2 两次 eval 会留下漂移

现有 `_INCR_WITH_TTL` 只做 INCR + PEXPIRE，一次一个键。Agent 闸门需要「读-比较-写」跨两个键，
分两次 `eval` 时进程若崩在中间，会留下「agent 加了、租户没加」或相反，
配额从此对不上，而且没有任何告警会报出来。

所以这一段要合成一个 Lua 脚本，返回结构化结果而不是抛异常：

```lua
-- KEYS[1] = agent 键, KEYS[2] = 租户键
-- ARGV[1] = agent 上限（空串表示不限）, ARGV[2] = 租户上限, ARGV[3] = TTL 毫秒
local agentLimit = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')

if agentLimit and agentLimit >= 0 and current >= agentLimit then
  return {0, 'agent'}          -- 超限，两个键都不动
end

local nextAgent = redis.call('INCR', KEYS[1])
if nextAgent == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[3]) end

local tenantCount = redis.call('INCR', KEYS[2])
if tenantCount == 1 then redis.call('PEXPIRE', KEYS[2], ARGV[3]) end

if tenantCount > tonumber(ARGV[2]) then
  return {0, 'tenant'}         -- 注意：agent 计数已经加了，见下文
end

return {1, ''}
```

**租户超限时 agent 的计数是已经加过的**，这一格是刻意留的：回滚要再写一次
`DECR`，而崩溃窗口就从「两次 eval 之间」挪到了「DECR 之前」，没有变好。
让 agent 多记一次无害（它本来也要被拒），让租户少记一次才是有害的。

## 6. 拒绝要说清是被哪一层挡的，并且分两类记账

现有 `ToolPermissionDeniedError.reason` 有四个取值（`scope` / `role` / `tenant` / `agent`），
配额不属于其中任何一个——它判的不是「你能不能」，而是「还有没有余量」。加第五个：

| 情形 | 异常 | 审计事件 | 算治理信号吗 |
|---|---|---|---|
| 每分钟速率超限 | `ToolRateLimitedError` | `tool_rate_limited` | **不算** |
| 每日配额耗尽 | `ToolDenied(reason="quota")` | `tool_denied` | **算** |

**为什么必须分开。** 速率超限是流量抖动，一天可能几十上百次，混进 `tool_denied`
会把 `reason=agent`（边界配窄了，真正需要人改配置的信号）淹掉。
日配额耗尽一天最多一次，它必然是配置或容量问题，必须能数、能按 Agent 排。

观测侧新增 `tool_blocked_by_quota`，与 `tool_blocked_by_agent` 并列。

### 6.1 判断顺序：权限在前，配额在后

```
agent_denial()（权限） → 配额闸门 → _pre_execute()
```

先判权限再判配额。反过来的后果：一个本来就没权限的调用会先消耗配额，
「越权尝试」变成「配额流失」，两件事都说不清了。

## 7. 数据模型：加列，不建表

在 `agents` 表加四列，不新建 `agent_quotas`：

```python
rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
daily_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
daily_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
daily_cost_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

三个理由：

1. **生命周期一致。** 配额是 Agent 的属性，独立表会留下孤儿行（Agent 删了配额还在），
   而这类孤儿没有任何清理时机是自然的。
2. **读路径在每次工具调用上。** 执行时已经加载了 Agent 行，同表读不需要额外查询；
   独立表要么 join、要么再维护一层缓存。
3. **不参与 `revision`。** 见下。

### 7.1 配额变更不 bump `revision`，也不进 `AgentBoundary`

`AgentBoundary` 是**权限收窄**的冻结对象，会随 state 进图、被审批单快照下来。
配额是**资源限制**，不是权限：

- 若配额进 boundary，改一个配额数字就会 bump `revision`；
- 于是所有在途审批单在 `_execution_agent_boundary()` 复核时会认为「边界变了」，
  按 `AGENT_BOUNDARY_CHANGED` 拒绝——只是调了个配额，把排队中的审批全废掉。

所以配额**不进** `AgentBoundary`，由网关在调用时按 `agent_id` 取（或由
`rate_limit_service` 单独查一次）。这个边界要写进代码注释，否则下一个改这块的人
很容易「顺手把新字段塞进 boundary」。

## 8. 接口与可见性

| 入口 | 变更 |
|---|---|
| `PATCH /api/v1/agents/{ref}` | 新增四个可写字段；**显式 `null` = 取消限制**（沿用 `model_fields_set` 区分「没传」与「传 null」的既有写法） |
| `GET /api/v1/agents/{ref}` | 返回四字段 + 当前窗口已用量 |
| `GET /api/v1/observability/agents` | 每行加 `limits` 与 `usage`，让「还剩多少」可见 |
| 控制台「智能体」页 | 边界摘要区加一行配额；超限状态标红 |

**只设不看等于没设。** 这一条不是界面美化：配额的价值在于「有人能提前发现某个 Agent
正在逼近上限」，而不是「到了上限系统自己拒绝」。所以观测接口必须给余量，
只给读数不给上限的报表，读者无法判断该不该动手。

## 9. 迁移与兼容

一条迁移，四列 `nullable=True`，无回填。

存量行全部 `NULL` → 走 `is None` 分支 → 行为与上线前逐字相同。
这与 `BASELINE` 那条兼容红线同构：**升级不改变任何既有会话的行为**。

不需要开关控制：不配就是没开。唯一需要开关的是「日配额耗尽是否记 `tool_denied`」，
因为它改变审计表的行数分布，接审计告警的人会被惊到一次。

## 10. 分期

| 期 | 范围 | 为什么排这个序 |
|---|---|---|
| P0 | 调用速率（次/分钟）+ 每日轮次 | 都在请求前可判，不依赖结算，最不容易给出错读数 |
| P1 | 每日 token | 需要从 `obs_traces` 累加，要先确认缺报比例 |
| P2 | 每日成本 | 现在 `cost_micros` 有大量 `null`（见 `/observability/agents` 的实际返回），拿缺口数据算上限会凭空多出余量 |

P0 的验收用例（每条都要能反向验证）：

1. A 限 1/分钟、B 不设限：A 打满后 B 的调用成功，租户闸门不触发。
2. `daily_turns=0`：第一次对话就被拒，`reason=quota`，审计有 `tool_denied`。
3. `daily_turns=None`：一直放行到租户上限，拒绝时 `reason=tenant`。
4. 改配额：`revision` 不变，在途审批单仍可执行。
5. 无权限的调用：先被 `agent_denial` 拒，Redis 计数为 0（不消耗配额）。
6. 并发 10 个请求打同一个 `daily_turns=1`：只有 1 个通过（验证 Lua 的原子性）。

## 11. 已知边界

- **name 复用会串计数。** Agent 删除后同名重建，若跨日窗口未过，新 Agent 会继承
  旧计数。缓解：键 TTL ≤ 25 小时，风险窗口最长一天；不主动删键（主动删会让
  「停用→启用」变成重置配额的后门）。
- **成本是软上限。** 超支幅度约等于单次请求成本，见第 3 节。
- **默认 Agent 不宜设额度。** 见 2.3。
- **叠加式不保证总量可控。** 见 2.1，这是有意的。
- **配额不能代替审批。** 它限制「做多少次」，不判断「这一次该不该做」。

## 12. 暂不做

| 事项 | 为什么 | 什么时候再看 |
|---|---|---|
| 预扣 + 结算（按历史 p95 预扣，结算后多退少补） | 复杂度高，且 p95 本身要稳定样本；现在的样本量还不足以支撑 | 有 Agent 真的把成本上限打爆过一次 |
| 配额自动调参（按趋势自动放宽/收紧） | 自动调额的治理风险远大于收益：谁批准的放宽？ | 不打算做 |
| 跨 Agent 共享配额池（A 和 B 共用一个额度） | 池子的语义在库里难以表达，且会让「谁用超了」不可追溯 | 出现「同一职责拆成两个 Agent」的真实需求 |
| 配额告警（80% 时推送） | 依赖先有观测余量字段 | P0 上线后 |

## 13. 相关文件

| 位置 | 会改什么 |
|---|---|
| `app/models/agent.py` | 四列 |
| `app/services/rate_limit_service.py` | agent 键、合并 Lua、`is None` 与 `0` 分开 |
| `app/tools/gateway.py` | 配额闸门位置（权限之后、执行之前） |
| `app/services/agent_service.py` | `AgentBoundary` 明确**不**收配额字段，写注释 |
| `app/api/routes/agents.py` | PATCH 四字段 |
| `app/api/routes/observability.py` | `/agents` 返回 limits / usage |
| `tests/unit/test_agent_quota.py` | 第 10 节六条验收 |
