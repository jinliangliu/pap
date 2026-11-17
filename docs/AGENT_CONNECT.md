# 智能体之间的连接与数据互通（设计稿）

> **状态（2026-11-12）：P0「连接」、P1「引用」、P2「写操作委派」均已实现。**
> 落地位置见 §10。控制面（能不能把一件事交给另一个 Agent、权限怎么收窄）的规则以
> [`A2A.md`](A2A.md) 为准，本文不重写那套规则，只补它没覆盖的三件事：**能力目录、
> 委派审计、引用传递**，以及 P2 的**审批单怎么跨两个线程走**（§5.2）。
> 数据面（什么数据能过桥、在谁的权限下判定）是本文的主体。

## 0. 一句话

连接解决「找得到谁」，数据互通解决「给得起什么」。两件事必须分开做：
先做连接，因为**每一条数据的可见性都要在一个正在执行的 B 的上下文里判定**——
委派提供了那个上下文，没有它，数据互通只能退化成共享池。

---

## 1. 对 `MULTI_AGENTS_Collaboration.md` 的评估

### 1.1 三个总判断

**一、它是一份合格的检查清单，不是可执行的方案。**
覆盖面很全（任务信封、结果结构、四种协作模式、四层授权、观测字段、五类指标、失败语义），
但每一节都停在「应该」：没有一处回答「以我们现在的表结构和执行图，第一步改哪个函数」。
可以拿它当评审 checklist，不能当 roadmap。

**二、它的阶段顺序对我们正好是反的。**
它把「高风险操作治理」放在第 4 阶段（最后），而审批、预览、回读核验、边界快照
是**我们已经有的最厚的一层**；它把任务队列、事件总线放在第 2、3 阶段，
而这两样在本平台没有消费方（单机、同步子调用、事件由场站平台产生）。
文档假设的是一张白纸系统；我们的问题是反过来——治理层已经很厚，缺的是通道。

**三、它没有回答「数据互通」。**
全文讲的是任务怎么传、结果怎么回。数据只在两处出现：`data_classification` 与 `payload_ref`，
两处都没有定义。而「引用」恰恰是整件事的枢纽——它假设 `payload_ref` 已经存在，
我们这里不存在。所以照着它做，会得到一个「能委派但什么也带不过去」的系统：
B 拿到的只有一段 `task` 文本，用户还得把工单号再说一遍。

### 1.2 可以直接采纳的（五条，已落在我们的设计里）

| 文档主张 | 我们这边的对应位置 |
|---|---|
| 不传聊天记录，传任务契约 | `A2A.md` §7，且理由更硬：是数据边界，不是省 token |
| 四层授权、禁止「协调 Agent 持有万能 Token」 | 有效权限 = `人 ∩ A ∩ B`，`gateway.py` 落地 |
| 超时 ≠ 未执行；`submitted` / `unknown` 要单列 | `A2A.md` §11 失败语义表 |
| 一条 trace、委派是一个 span；不要 `parent_trace_id` | `A2A.md` §10，连拒绝理由都一样：求和口径会当场失效 |
| 循环防护：最大深度、禁回边、任务指纹、预算 | `A2A.md` §8（深度 2 / 单轮 3 次 / `delegation_path`） |

### 1.3 不能采纳的（六条）

| 文档主张 | 为什么不采纳 |
|---|---|
| 协调层 / Orchestrator 作为一等组件 | 我们的图是固定 7 节点（`app/agents/graph.py`），模型在 `plan_request` 里选工具。加协调层等于把「什么时候该委派」写进代码，与「Agent 是配置」直接相悖。委派做成**工具**，由模型选 |
| 能力卡手写并入库 | 我们的能力是**算出来的**（人 ∩ agent ∩ 工具注册表），不是声明的。手写卡在第一次改 `allowed_tools` 后就过期——这个坑刚在 `sync_studio_definition` 上踩过：建行时把 `allowed_tools` 写死，之后加工具对老租户全部无效，表现成「助手拒绝它其实能做的事」 |
| `expected_output_schema` + 版本兼容矩阵 | 第一阶段没有 schema 注册表与校验器。强制 schema 会把「模型格式偶尔不对」升级成「任务失败」，而失败比格式错更难恢复。先只要求**字段存在**，不校验类型版本 |
| `uncertainty.confidence` 数值 | 模型自报的置信度没有校准意义，而下游一定会拿它做阈值判断。改成「证据 + 缺失项」两段，由人判断。这是明确分歧，不是遗漏 |
| `data_classification` 字段 | 没有消费方的分类字段会永远填 `internal`。我们已经有能判定的东西：知识库的 `access_scope`、artifact 的 `user_id` / `conversation_id`。**可见性即分类** |
| 任务队列 / 事件总线 / 发布订阅（其阶段 2、3） | 没有消费者。真要做异步，前置条件是「有任务稳定超过一次 HTTP 时限」，现在没有。为一个不存在的量级引入三套基础设施，是负资产 |

### 1.4 它说对了但说浅了的一处

文档 §1 的架构图与 §6 的授权模型**自相矛盾**：协调层要「汇总答案」就必须看到所有子结果，
于是它事实上持有了一个万能视图；§6 又说不要给协调层万能 Token。文档没有解这个矛盾。

我们的解法不是「给它权限但要求它不用」，而是**结构上没有那个位置**：
不存在汇总者。A 只拿到 B 允许外传的那一部分（见 §4 结果信封），
裁剪发生在 B 那一侧。A 想要更多，只能显式再委派一次。

---

## 2. 连接（控制面）：落地 `A2A.md` + 两处增量

委派本身的规则（子集判定、深度、环、预算、失败语义）全部按 `A2A.md` 执行，不重写。
这里只补它没写的两件事。

### 2.1 能力目录：`GET /api/v1/agents/{ref}/capabilities`

解决连接的第一个问题：**A 不知道能找谁，只能按名字猜。**

**运行时生成，不落库、不可编辑**，来源是已有数据：

| 字段 | 来源 |
|---|---|
| `name` / `display_name` / `description` | `agents` 表 |
| `tools` | `allowed_tools` ∪ `allowed_connectors`（均为 `None` 表示不额外收窄）∩ 调用者可见的工具目录 |
| `connectors` | `allowed_connectors`（排序后的名字） |
| `skills` | `allowed_skills` ∩ 调用者可见的技能 |
| `can_write` / `status` / `revision` | `agents` 表 |
| `delegatable` | `status == active` 且非默认 Agent 且非内置 Agent（`A2A.md` §6） |

两条约束：

- **按调用者的可见性求交**，不是列出该 Agent 的全部能力。否则这个接口会变成
  「用它探测别人有什么工具」的情报接口。
- 生成结果里**不含** `instruction`。职责说明是内部作业约束，
  给 A 看等于让它去模仿 B 的口吻。

### 2.2 委派审计：`agent_delegated`

`obs_traces` 有保留期（`obs_retention_days`），审计是长期账。
「这个 Agent 被谁叫去干过活」必须能在 trace 过期后仍然回答，且 Agent 删除后仍能反查
（`resource_id` 用 UUID，与 `agent_deleted` 同一口径）。

| 字段 | 值 |
|---|---|
| `event_type` | `agent_delegated` |
| `resource_type` / `resource_id` | `agent` / **接收方的 name** |
| `metadata` | `from_agent`、`from_revision`、`to_agent`、`to_agent_id`、`depth`、`status`、`reject_reason`、`task_chars`、`task_sha256` |

两处口径：

- `resource_id` 用**名字**不用 UUID：名字创建后不可改、租户内唯一，同样能反查；
  而深度/环/预算这些拒绝发生在查库之前，那时手上只有名字。一列里混两种标识，
  查询早晚会写错。UUID 放 `metadata.to_agent_id`，需要精确定位时用它。
- `task_sha256` 而不是 `task` 原文：任务描述里可能带业务细节，而审计表是长期保留的。
  委派**不审批**（`A2A.md` §6），但审计和审批是两件事——前者记账，后者拦人。
- 被拒绝的委派也要记（`reject_reason` 是 `depth` / `cycle` / `budget` /
  `allowed_tools:t3` 这类具体项）。`A2A.md` §1 说的正是「事后查审计，没有任何一条记录
  说不该走到这里」——那条缝要在这里补上。

---

## 3. 数据互通：三条通道，一种判定

先把话说死：**互通不是共享一个池子。** 共享池 = 谁都能读 = 权限模型当场失效。
互通是「**在 B 自己的身份下，重新判定一次可见性**」。

| 通道 | 传什么 | 谁判定 | 现状 |
|---|---|---|---|
| 共享读 `shared` | 什么都不传，B 自己去读 | B 的工具目录 / 知识检索，按 B 的身份 | 已有（`access_scope`、工具目录） |
| 引用传递 `ref` | `{kind, id}` | B 侧 resolve 时判定 | **P1 已实现**（`reference_service.py`） |
| 结果回传 `envelope` | 结论 + 引用 + 缺失项 | B 裁剪，A 只能看到这些 | **P1 已实现**（`_evidence_of`） |

三条落地方式（与「设计」的差别都在这里，读完这三条就不用再去翻代码猜）：

- **`refs` 是工具入参，不是隐式继承。** 不给 `refs` 时子调用一个附件都没有——
  附件句柄是会话级的，默认不过桥。这一条必须能从代码里证伪：
  `test_no_ref_means_no_attachments`。
- **产物是复制过去的，不是递路径。** 子调用的附件读取只认「本租户 + 本会话目录里的句柄」，
  递对象键过去，B 拿到的是一个读不了的值，而报错是「附件不存在」——比不给还难查。
  代价是同一份 PDF 转交三次就是三份文件；它落在会话目录下，会话清理会连目录一起回收。
- **只有 PDF 与图片会被复制。** 其它类型复制过去也没人能用（没有读取工具），
  反而会让下游以为「有附件就一定能读」。退化成只给元数据，且材料块里不出现 `file_id`。

### 3.1 四种引用，借现有主键，不建新表

| `kind` | `id` | 可见性判定 | 失效语义 |
|---|---|---|---|
| `artifact` | `artifacts.id` | 同租户 + 同 `user_id` | 行不存在 / 文件丢了 → `REF_GONE` |
| `message` | `messages.id` | 同租户 + 会话所有者 = `user_id` | 会话被清理 → `REF_GONE` |
| `knowledge` | 文档 id（+ 可选 `section`） | `access_scope` ⊆ B 的 scope（与 `retriever.py` 同一判定）+ `visibility` + `allowed_roles` | 文档下架 / 未就绪 → `REF_GONE` |
| `entity` | 业务标识（`station_id` 等） | 交给该业务工具自己的授权（站点授权） | 业务侧 404 |

**不建 `agent_refs` 表。** 引用是一次性入参，不是长期关系。落表就要处理级联删除、
孤儿行、保留期，而我们只需要回答「这一跳传了什么」——这个写在审计 metadata 里。

三处与原设计的偏差，都是实现时才看清的：

- **`artifact` 不再限 `conversation_id`。** A 与 B 继承同一个会话，限它挡不住任何东西；
  它只会把「引用上一轮会话里那份报告」一起挡掉，而那是同一个人自己的材料。
  真正起作用的判定是 `user_id`。
- **`message` 的所有者取自 `conversations.user_id`。** `messages` 上没有用户列，
  「这条消息属于谁」唯一可靠的答案在它所属的会话上。
- **`entity` 是唯一不做本地判定的类型。** 在这里再判一次站点授权，会得到两套授权，
  而它们早晚不一致——一致时没人发现，不一致时表现成「引用过了、调用却被拒」。

一次最多 10 个引用（`MAX_REFS`）。与任务描述的长度上限是配套的：再多的已经不是
「交给对方一件事」，而是让对方读一座库——那件事应该由 B 自己去检索，或者由人拆成几轮。
重复的引用会被合并而不是报错：A 看不出来自己把同一份材料写了两遍，
报错只会让人以为引用机制坏了。

### 3.2 判定必须是三态，不能塌成二态

```
可见        → 返回内容/句柄
不可见      → REF_NOT_VISIBLE（带缺哪个 scope）
已失效      → REF_GONE
```

**不能把「读不到」返回成空内容。** 这是 RLS 那个坑的教训：裸 SQL 查带行级安全的表
永远 0 行，「清干净了」与「看不见」同形，我们据此误报过一次。
同理会发生在 ref 上：B 收到空，会当成「A 那边没有这个材料」，
然后编一个看起来合理的结论——错误被静默传播，且没有任何一条日志。

拒绝理由要**点名缺什么**（`missing_scope=ticket:read`），否则排障只能看到「引用不可用」。

### 3.3 部分成功是非法的

A 传 5 个 ref，1 个不可见 → **整单拒绝**，不静默丢 1 个。
部分成功会让 A 以为 B 看到了全部，而 B 只看到 4 个还会照样给出「完整」的结论。
这比直接失败危险得多。

落地方式就是「遇到第一个失败就抛出去」：`resolve_refs` 没有 `try/except`，
调用方 `delegate_to_agent` 捕到就整单返回失败，子图一次都不跑。
反过来写（`except: continue`）在代码上更短，而且所有 P0 用例都会保持绿色——
唯一的变化是每次转交都「静默地少带了材料」。所以这一条必须有一条能证伪的用例：
`test_one_bad_ref_rejects_the_whole_delegation`。

三态在接口上就是三个 `error_code`：`REF_NOT_VISIBLE` / `REF_GONE` / `REF_INVALID`，
前两个还会带 `kind` + `id`，`REF_NOT_VISIBLE` 额外带 `missing_scope`。
`REF_INVALID` 不是第四态——它说的不是「这份材料怎样了」，而是「这条请求根本没法判」。

**顺序是刻意的：纪律 → 目标 → 边界 → 引用。** 前三条都是「这次转交根本不该发生」，
引用是「转交成立，但材料给不过去」。反过来写的话，一次越权转交会先花几次
数据库查询去解析材料，然后把真正的原因（边界太宽）藏在第二条错误里。

---

## 4. 结果信封：最小四段

```json
{
  "task_id": "task_01H...",
  "status": "completed",
  "summary": "3 号泵昨日 14:20 跳停，压力越限触发保护。",
  "structured": {"station_id": "T002509008", "conclusion": "sensor_or_protection"},
  "evidence": [
    {"kind": "knowledge", "id": "doc_...", "section": "4.2", "quote": "压力超过阈值 0.8MPa 时联锁停机", "observed_at": "2026-11-12T10:47:10+08:00"},
    {"kind": "artifact", "id": "3f2a..."}
  ],
  "unknown": ["未获得现场阀位反馈"]
}
```

三条约定：

- `evidence[]` 的元素是**引用**，不是正文。允许 `quote` 短摘录（≤200 字），
  因为纯 id 会让 A 无法转述；但全文必须靠 ref 取，取的时候再判一次可见性。
- `unknown[]` **字段必须存在**（可为空数组）。模型最擅长的就是省略，
  而「省略」与「没有缺失」在结构上无法区分。
- 没有 `confidence`。见 §1.3。

A 拿到信封后只能转述 `summary` 与 `structured`，不能替 B 扩写证据——
扩写就是在没有权限的地方生成内容。

**实现里只做了 `summary` / `evidence` / `unknown` 三段**，另外多带三个字段：

| 实际返回 | 为什么 |
|---|---|
| `agent` / `display_name` | A 需要知道是谁答的；转述时点名是「谁说的」，不是「事实如此」 |
| `readonly` | B 这一跳是不是只读的。P2 之后它不再恒为 True：发起方要能分清「它做不了是职责限制」还是「这一层被关掉了」（§5.1） |
| `depth` | A 判断还能不能再转一跳 |

`task_id` 与 `structured` **没做**：前者没有消费方（子调用不写 `agent_runs`），
后者在 §1.3 里已经论证过——第一阶段没有 schema 注册表，强制结构化会把
「模型格式偶尔不对」升级成「任务失败」，而失败比格式错更难恢复。
`evidence[]` 目前只出 `knowledge` 与 `tool` 两类，`quote` 也没回传：
A 拿到的应该是「凭什么」，不是一份可以再分发的内容副本。

---

## 5. 身份：用户继承，边界取 B 的

这是整件事最容易写错的一条，写错了前面所有校验都白做。

| 维度 | 委派时取值 | 不这样会怎样 |
|---|---|---|
| `user_id` / `scopes` / `roles` | **发起人**（继承） | 换成 B 的服务身份 = 需要给 B 发凭据 = 回到「万能 Token」 |
| `agent_*`（id/name/revision/allowed_tools/can_write） | **B 自己的** | 带 A 的边界 = 没有委派，只是 A 换了个提示词 |
| `thread_id` / `conversation_id` | 继承（同一次请求） | 子调用另起会话会让产物、附件、配额全部错位 |

因为强制 `B ⊆ A`（`A2A.md` §3），有 `人 ∩ A ∩ B ≡ 人 ∩ B`——
所以运行时**只需要带 B 的边界**，不必把 A 的白名单序列化进子调用。
这不是偷懒，`A2A.md` §4 已经论证过：传丢了只会更严，不会更宽。

**这个等式有一个前提，容易被写成结论。** 它只在 B **显式收窄**时成立。
`B.allowed_tools = None` 的含义是「我不额外收窄」，此时 `人 ∩ A ∩ B` 会是 `人 ∩ A`，
而运行时给的是 `人 ∩ B`——两者不等。所以子集判定对 `None` 的处理是**放行**，
并且承认语义：委派是「换一个更专业的人做」，不是「给同一个人加权限」。
真正守住的那条是 `can_write` 单调（只读的 A 不能转交给可写 B），
它挡的是一整类风险等级，不是名单里的几个名字。

另一种做法是「在父级算好 `A ∩ B` 再下传」，能让等式恒成立。**没选它**，因为
A 收窄到三个工具、B 是全量配置时，下传交集会把 B 需要的工具也砍掉——
委派的用途当场消失，只剩一个更啰嗦的 A。

`allowed_connectors` 把同一套判断延伸到连接器，并且**多一条保守规则**：
`sub.allowed_connectors` 非空、`sup.allowed_tools` 已经收窄、但
`sup.allowed_connectors is None` 时**拒绝**。纯函数看不到连接器下面挂着二十个工具，
放行的代价是「A 只给了三个工具，B 凭一个连接器名拿到二十个」；
拒绝的代价只是「父级要么显式列出允许的连接器，要么别收窄工具」。
这两者不对称，所以取严的一侧（规则与实现见 `docs/CONNECTORS.md` §5）。

反面用例（必须能测出来）：A 白名单含 `t2`、B 不含，B 调 `t2` 必须被拒。
如果实现里为省事把 A 的边界传给了 B，这条会红，而其余用例全绿。

### 5.1 子调用从只读到可写（P2）

P0 时子调用一律以 `agent_can_write=False` 运行，原因不是保守，是 `interrupt()` 的位置：
审批靠「图在审批节点中断、恢复后从断点继续」，而子调用发生在**一个工具处理函数内部**，
中断只能以异常形式从工具里冒出来；父图并不知道这个断点存在，恢复时会从父节点重跑，
那个工具调用被整体重放一遍。

P2 没有去解决「嵌套中断」，而是**不去嵌套**：

| 问题 | 做法 |
|---|---|
| 子图停在审批上怎么办 | 父图不中断。子图返回的 `__interrupt__` 被读出来，作为「对方在等确认」的工具结果回到 A |
| 子线程的检查点怎么办 | **留着**（只读委派是当场删）。确认 / 取消 / 过期三条终态上才清理 |
| 恢复哪个线程 | 子线程（`approval.thread_id`）。单子上另存 `parent_thread_id` 指向发起方会话 |
| 结论写到哪 | 发起方会话，并点名「谁做的」 |

于是「一次写操作两次确认」没有发生：确认还是那一次，只是这张卡的抬头上多了一句
「在谁的委派下」。而父图从头到尾没有中断，也就不存在「恢复时重放整个委派」。

**`can_write` 仍然是单调的**：子调用取 `boundary.can_write`（B 自己的值），而子集校验
已经要求「B 可写 ⇒ A 可写」。所以放开的是「可写的 A 转交给可写的 B」，
不是「只读的 A 借道变可写」——后一种在委派的第一步就被拒。

### 5.2 审批单跨两个线程

```
A 的线程 ──delegate──▶ B 的子线程（thread_id = 父:d1:ops:xxxx）
                           │  create_and_wait_approval → interrupt
                           ▼
                      审批单 PENDING
                      thread_id       = 子线程   （恢复用）
                      parent_thread_id= A 的线程 （写结论用）
                      delegation_chain= [A]      （复核用）
                           │  用户点确认
                           ▼
              resume(子线程) → B 执行 → 结论写进 A 的会话
```

三列各有各的用途，缺一个都会坏在具体的地方：少了 `parent_thread_id`，结论写到一个
不存在的会话上（日志里只有一句「定位不到会话」）；少了 `delegation_chain`，
批准期间 A 被收窄就拦不住；少了 `thread_id` 根本没有恢复点。

---

## 6. 观测与审计

- **一条 trace。** 每个 Agent 执行是一个 `kind="agent"` span，
  `attrs = {agent, revision, depth, delegated_from}`。`obs_traces.agent_name`
  仍是发起者，`traces` 口径不变（`A2A.md` §10）。
- `delegated_turns` 由 `kind=agent && depth>=1` 的 span 数出来。
- 「谁被叫去做事」与「谁发起」是两个数，**不能相加成一个**。
- 依赖图从 span 的 `delegated_from` 聚合，不建新表——
  文档 §8.E 的「Agent 依赖图」到这里才有数据。

---

## 7. 分期与验收

| 期 | 范围 | 验收 |
|---|---|---|
| **P0 连接（已实现）** | `A2A.md` P0 落地；能力目录 `GET /agents/{ref}/capabilities`；`agent_delegated` 审计；`plan_request` 注入可转交清单 | `A2A.md` §14 前七条 + 本文 §7.1 的 1、2；共 23 条用例 |
| **P1 引用（已实现）** | `delegate_to_agent` 增 `refs` 入参；四类 ref resolve；三态拒绝；材料块与附件转交；无证据不过桥 | 本文 §7.1 的 3–8；共 29 条用例（`test_agent_references.py` 28 + `test_agent_delegate.py` 1） |
| **P2 写操作委派（已实现）** | `A2A.md` §9：整条 `delegation_chain` 冻结、执行期逐跳复核、`DELEGATION_REVOKED`；子调用解除只读；子图停在审批上时保留检查点、由 `parent_thread_id` 把结论送回发起会话 | 本文 §7.1 的 9–12；共 13 条用例（`test_delegated_approval.py` 7 + `test_delegated_write.py` 6） |
| **P3-a 预算（已实现）** | `agents.max_delegation_depth` / `delegation_budget`，沿链取 min | `A2A.md` §14 第 14–16 条；`test_delegation_limits.py` 14 条 + `test_agent_delegation.py` 6 条 |
| **P3-b 指标（已实现）** | `/observability/agents` 的 `delegated_turns`，取 `obs_spans` 的 `kind=agent` | `A2A.md` §14 第 17 条 |
| **P3-c 产物（已实现）** | `save_artifact` 工具 + 产物绑会话 + 引用按会话收敛 | `A2A.md` §14 第 18 条；`test_artifact_tool.py` 12 条 + `test_agent_references.py` 3 条 + `test_artifacts_api.py` 2 条 |

### 7.1 验收用例（每条去掉实现后必须失败）

1. 能力目录里不出现调用者看不到的工具。
2. 委派成功 → 审计有一条 `agent_delegated`，`resource_id` 是 **B 的 name**（UUID 在 `metadata.to_agent_id`，理由见 §2.2），`metadata.refs` 只有 id 没有正文。
3. A 传 `artifact` ref，B 的 `user_id` 不同 → `REF_NOT_VISIBLE`，**不是空内容**。
4. 知识文档 `access_scope` 高于 B 的 scope → `REF_NOT_VISIBLE`，理由点名缺的 scope。
5. 删除 artifact 后再 resolve → `REF_GONE`。
6. A 传 5 个 ref，1 个不可见 → 整单拒绝（不允许部分成功）。
7. B 返回的结论无任何依据 → 任务失败（不允许无证据结论过桥）。
8. 深度 2 时 ref 仍可解析（引用不随深度衰减，但每跳都在 B 的上下文里重判）。

第 7 条的判据要写死，否则实现会走偏成「必须有 `citations`」：
**没调用过工具 + 没声明缺什么 + 发起方也没给材料**，三样都没有才拦。
判据写成「必须有 `citations`」的话，「基于 A 给的材料作答」这一整类正常转交都会被拦——
而这恰好是最该工作的那一类。反向用例是 `test_same_answer_passes_when_materials_were_given`：
同一条回答，只要发起方给过材料就放行。

拒绝时给的话术要能行动：「请在任务里明确要求它先检索或先说明缺什么，再转交一次」。
A 本轮还有预算（单轮 3 次），所以这条失败是可恢复的，不是死路。

P2 的四条：
9. 委派写操作停在 **B 的审批**上：响应是 `awaiting_approval`（父图没有中断），
   审批单上 `delegated_from=cs`、`delegation_depth=1`、`parent_thread_id` 有值。
10. 批准期间把 A 收窄到不再包含 B 要用的工具 → 执行被拒，`error_code=DELEGATION_REVOKED`，
    `executed_at is None`（外部系统一次都没被写）。
11. 确认后结论落在**发起方**会话：那条会话有三条消息（问 / 等确认 / 结论），
    最后一条带 `approval_action=confirm` 并点名是 B 做的。
12. 反向：用户自己发起写操作时 `delegation_chain IS NULL`、`parent_thread_id IS NULL`，
    正常执行——复核链路不能对非委派单生效。

第 9–12 条都在 `test_delegated_write.py`；第 10 条的两跳形式（A 收窄、B 未变）
在 `test_delegated_approval.py`，因为那是「只复核直接上级」唯一会漏的那一类。

### 7.2 实机验证记录（2026-11-12，生产库）

自动化之外，在服务器上真跑了一遍（真实模型、真实 PG）。确认到的：

| 项 | 结果 |
|---|---|
| 委派闭环 | `cs`（只给 `delegate_to_agent`）转交 `ops`，用户看到的是 ops 的结论；审计 `resource_id=sa_ops`、`depth=1`、`status=completed`、`task_sha256` 有值而原文没有 |
| 越权拒绝 | 把 `ops` 改成 `cs` 没有的两个工具后点名转交 → 当场拒绝并点名 `get_platform_time`，子调用没跑；审计 `reject_reason=allowed_tools:get_platform_time` |
| 引用传递 | 上传一份 PDF、以 `refs=[{kind:artifact,id:…}]` 转交 → 子调用真的对它做了 OCR（空 PDF 得 `line_count=0`），答复里报出了文件名与 `file_id` |
| 三态 | 不存在的 id → 「产物已不存在」并落 `reject=ref:REF_GONE`，与「无权看」是两个错误码 |
| 整单拒绝 | 一个好引用 + 一个坏引用 → 整单拒绝，好那份也没送过去；审计把**两个 id 都记下**（被拒的那次最值得查） |
| RLS | `connector_credentials` 在 PG 上 `rowsecurity=true`、`forcerowsecurity=true`，策略 `tenant_id = current_setting('app.tenant_id', true)`；应用角色 `pap_app` 非超级用户，裸查 `audit_logs` 返回 0 行 |

四条容易误判的（详见 `MANUAL_TEST.md`）：`ops` 必须建成 `can_write=false`，否则会被
子集校验排除出可转交清单，模型会说「没有清单」；问题里要给全站点与日期，否则第一轮
只是在反问；审计接口不回 `metadata`，且库里 `event_type` 是全大写；SA28 的回环护栏
不靠模型——它常常自己就拒绝了，确定性验证在单测。

### 7.3 P2 实机验证记录（2026-11-12，生产库 57f24ea）

自动化之外，在生产上真跑了一遍（真实模型、真实 PG、`create_maintenance_ticket` 的
演示模式）。建了两个可写智能体：`cs`（`["delegate_to_agent","create_maintenance_ticket"]`）
与 `ops`（`["create_maintenance_ticket"]`）。确认到的：

| 项 | 结果 |
|---|---|
| 委派写操作闭环 | `cs` 转交 `ops` 建工单 → `status=awaiting_approval`，`preview.delegation={from:cs,depth:1}`；确认后 `VERIFIED`（演示模式回读）。发起会话里**三条**消息，最后一条 `approval_action=confirm` 且点名「运行值班」 |
| 审批单冻结了什么 | `delegation_chain=[{id:8b0cd56b…,name:cs}]`、`parent_thread_id=default:…:m-p2-sa36`；`APPROVAL_CREATED` 的审计 metadata 里有同一份链与 `depth` |
| 委派审计 | `agent_delegated` 的 `status=awaiting_approval`、`metadata.approval_id` 有值——事后能回答「这次转交有没有变成一张待确认的写单」 |
| 收窄作废 | 批准期间把 `cs` 的 `allowed_tools` 去掉 `create_maintenance_ticket` → `FAILED` / `DELEGATION_REVOKED`，理由点名是 `cs` 收窄且点名那个工具；审批单 `executed_at` 为空 |
| 反向（非委派） | 默认智能体直接建工单 → `delegated_from=null`、`delegation_depth=0`、预览里没有 `delegation`，确认后照常 `VERIFIED` |
| 过期清理 | 把 `expires_at` 改到过去，等维护循环 → `EXPIRED`，子线程的 checkpoint 从 4 行变 0 行 |

**实机才发现的一处文案缺陷**：确认接口回的那段话，前缀跟着「用户点了确认」走，
于是作废那一单显示成「「运行值班」已完成你确认的操作。操作未执行：…… 」。
同一段话里自相矛盾，而读者只会记住前一句——正是「把没执行说成已完成」这一类。
改成读子图回传的 `execution_status`，并补了一条「`已完成` 不应出现」的断言
（去掉分支就会红）。这类事只有真跑一遍才会暴露：单测里那句前缀是被当作常量写的。

清理：两个测试智能体用完停用并删除，服务端智能体列表回到
`assistant` / `studio` / `Gas_Industry_Knowledge`。


---

## 8. 明确不做

| 事项 | 理由 |
|---|---|
| 协调层 / Orchestrator 组件 | 与「Agent 是配置」相悖，且会把决策写进代码，见 §1.3 |
| 手写能力卡入库 | 一定会过期，见 §1.3 |
| 任务队列 / 事件总线 / 订阅 | 没有消费者；引入即负资产 |
| 多 Agent 并发 / 辩论 | 单机跑并行 Agent 先把时延打爆，合并规则也没人定得了 |
| 跨租户委派与跨租户引用 | 租户是硬边界，不是权限问题 |
| `confidence` 数值、`data_classification` 字段 | 无消费方，见 §1.3 |
| 跨用户共享产物 | 那是产品决策（谁的产物能给别人看），不是协议问题。P3-c 只做到「同一个人的同一个会话」 |
| 跨会话取用产物 | P3-c 选的是**按会话保留**：绑了会话的产物只在那一场对话里可引用，随会话删除销毁。跨会话取用要另设保留期与清理 |

---

## 9. 已知边界

- **引用只保证「能被判定」，不保证「有意义」。** B 拿到一份自己有权读但与任务无关的
  工单，边界管不了这件事，只能靠 A 写清 `task`。
- **A 可以诱导 B 去做 B 有权做的事。** 这是委派的固有风险，子集校验拦不住，
  只能靠职责说明 + B 自己的审批。
- **证据链止于一跳。** A 看不到 B 为什么会得出这个结论——只看得到 B 给出的证据。
  要更深的可追溯性，得让 B 也回传它的委派链，那是 P2 之后的事。
- **成本归 B。** 与 `A2A.md` §15 一致：「谁在烧钱」必须记在真正执行的那一方。
- **删除 Agent 后引用不受影响。** ref 指向数据，不指向 Agent；Agent 删了，
  历史审计里的 `agent_delegated` 仍按当时的名字可读（`AGENTS.md` §5.3 的快照口径）。
- **默认 Agent 也能委派。** 它的 `allowed_tools = None`（不额外收窄），
  所以 `delegate_to_agent` 在目录里，提示词里也会列出可转交的同行。
  这是「不传 Agent 等于平台基线」的直接推论；要关掉，把它收窄成一份不含该工具的清单。
- **子调用不写 `agent_runs`。** 它没有独立的用户请求，写一条会让「这个会话跑了几轮」
  算错。代价是对话台看不到 B 的那一轮，只能从审计与 trace 里查。
- **子调用的检查点用完即删。** 结果是同步返回的，那个 thread 没有恢复价值；
  留着会在 checkpoint 表里攒下一堆不对应任何会话的孤立线程，而会话清理扫不到它们。
- **P0 的成本记在发起 trace 上。** `obs_traces` 是按 trace 汇总的，
  B 的 token 也计入 A 这条 trace。按 Agent 拆成本要等 `A2A.md` §13 的 P2。
- **转交产物是一份副本，不是一次共享。** 同一份 PDF 转交三次就是三份文件。
  它们落在子调用所在会话的附件目录下，会话清理会连目录一起回收，
  不会变成无人认领的垃圾——但这也意味着**一次转交就多占一份配额**。
  P3-c 之后这条仍然成立：**转交仍是一份副本**，改造的是「产物有没有归属」，
  不是「能不能直接共享文件」。共享对象是另一回事（对象键不是权限）。
- **只有 PDF 与图片会随任务带过去。** 其它类型只给元数据（文件名、类型、大小）。
  这不是省事：子调用那一侧只有 `ocr_document` 一个读取工具，
  复制一个 `.docx` 过去，B 会去调 OCR、失败、再解释一遍——比一开始就说没有更费。
- **无证据拦截会误伤「它其实知道」的情况。** B 一次工具都没调、也没声明缺什么，
  哪怕它答得对，也会被拦。这是刻意的：A 无法区分「知道」与「编得像」，
  而转述会把 A 的信用借给后者。代价是多一轮往返（本轮还有预算）。
- **委派来的写操作，结论不经过 A 的转述。** 确认发生在子线程上，B 的执行结论直接
  贴进发起方会话。A 只有上半轮（转交那一轮）能说话——它没法在结果上再补一句解释。
  这是「不要第二次确认」的代价，与 `A2A.md` §6 的立场一致：给委派加审批会让人闭眼点。
- **A 把话说满时，服务端只兜底一句。** 模型很容易把「已经转交」说成「已经办好」，
  所以这一轮的回答里一定会追加一句服务端生成的「还没写入」。它改不了模型的措辞，
  只能保证最后一句是真的——真正的状态在响应体的 `status` 与审批单上。
- ~~审批卡在刷新后会消失（对话台）。~~ **已修**（2026-11-12）：历史消息里留的
  `approval_id` 现在会经 `GET /api/v1/approvals?ids=` 批量取回，还原成同一张卡。
  能不能点由服务端的 `actionable` 决定（服务端时钟），不看浏览器时间；
  已处理的卡写结论并附 `error_code`，让「执行失败」与「被上游否决」在卡面上就分得清。
- **一张委派审批单只对应一次写操作。** 子线程的 `run_id` 恒为空，审批幂等键是
  `approval:{子线程}:`——同一子线程里的第二次写操作会复用第一次的单子。
  现在走不到（写操作之后图直接收尾），记在这里是因为它由「子调用没有独立 run」
  推出来，而不是由设计推出来。
- **B 拿不到 ref 的 id 之外的东西。** 材料块里只有 `kind:id` 与短摘录。
  想要别的内容，只能由 A 再显式引用一次——B 不能顺着一个 id 去翻 A 的其他材料。

---

## 10. 相关文件

| 位置 | 内容 |
|---|---|
| `app/tools/builtins/delegate_tools.py` | `delegate_to_agent`：纪律检查、目标解析、边界判定、**引用解析**、子图调用、**中断转「等确认」**、审计、信封 |
| `app/services/reference_service.py` | **P1 主体**：`parse_refs` / `resolve_refs`（四类）/ 三态异常 / `render_materials` / 审计摘要 |
| `app/agents/interrupts.py` | 中断载荷解析（父图与子图共用一份，两条形状都认） |
| `app/services/approval_service.py` | **P2 主体**：`delegation_chain` 冻结、`_review_delegation` 逐跳复核、`DelegationRevoked` |
| `app/db/checkpoint.py` | `discard_thread()`：只读委派当场丢弃，写委派在确认 / 取消 / 过期后丢弃 |
| `app/services/expiry_service.py` | 过期审批顺带清理委派子线程（否则每张过期单留一条孤立线程） |
| `app/services/agent_service.py` | `agent_subset()`（唯一判定）、`describe_violation()`、`delegatable_agents()`、`capabilities_of()` |
| `app/services/delegation_limits.py` | **P3-a**：委派预算的默认值、硬上限、`narrowed_by()`（取 min）、`check_limit()` |
| `app/tools/types.py` | `ToolExecutionContext.delegation_path` / `delegation_count` / `delegation_limits` / `agent_max_delegation_depth` / `agent_allowed_skills` |
| `app/agents/state.py` | 同名字段（跨图只能走 state，理由见代码注释） |
| `app/agents/nodes.py` | `_delegation_block()` 注入目标清单；`execute_read_tool` 记委派次数与路径 |
| `app/agents/planner.py` | `delegation_block` 拼在「角色职责」之后、「作业技能」之前 |
| `app/api/routes/agents.py` | `GET /{ref}/capabilities` |
| `app/models/audit.py` | `agent_delegated` 事件类型 |
| `app/knowledge/documents.py` | `acl_of()` 公开给引用解析用——「一份文档能不能被某个 Agent 看」必须与列表/检索走同一个判定 |
| `tests/unit/test_agent_delegation.py` | 子集、纪律、子调用上下文、审计、**委派预算**（24 条） |
| `tests/unit/test_delegation_limits.py` | **P3-a**：取 min、0 与 null 的区分、checkpoint 脏值（14 条） |
| `tests/unit/test_agent_references.py` | **P1**：入参校验、产物/消息/文档三态、整单拒绝、材料与附件、无证据拦截（28 条） |
| `tests/integration/test_agent_delegate.py` | 真实转交、越权拒绝、能力目录求交、`refs` 贯通接口层（9 条） |
| `tests/unit/test_delegated_approval.py` | **P2**：逐跳复核（两跳、删/停/收窄、无 id 的跳）7 条 |
| `tests/integration/test_delegated_write.py` | **P2**：委派写操作全链路、收窄作废、取消清理、非委派单不受影响（6 条） |
