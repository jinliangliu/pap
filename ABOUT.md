# About PAP

> **一句话**：智能体能动手，但每次动手前得有人点头。
>
> **In one line**: Agents that can take real actions — and a human has to say yes before every one of them.

PAP（Personal Agent Platform）是一套自托管的智能体平台。它不追求「让模型更聪明」，
只解决一件事：**一个组织里同时跑着好几个智能体时，怎么保证它们不乱来、出了事查得到。**
FastAPI + LangGraph + PostgreSQL。没有向量库，没有自动批准，没有 SaaS 后台。

## 为什么会有这个东西

自己拼的 agent demo 通常长这样：一段超长 system prompt，挂上几个工具，演示效果很惊艳。
真交给业务用，三个问题立刻冒出来：

**谁在做事？** 五个 agent 挤在一个进程里，没人说得清这次的结论是谁出的、下次还能不能复现。
PAP 把 agent 做成数据：有名字、有职责、有工具边界、有开关，在控制台上改，不用改代码。

**它能碰什么？** 一个 agent 又懂客服又懂运维，等于把两边的权限都给了它。拆开之后，
下游的权限从哪来？PAP 只有一个答案——**只能收，不能放**。
`人的权限 ∩ 每个 agent 的边界`，链条中间哪个被配宽了，也漏不到更下游。

**出事了谁认？** 一次写操作失败，你得回答：谁批的、当时参数是什么、对方系统到底执行了没有。
PAP 的写操作一律走两次请求：先给预览，人点了确认才真正执行，执行完再去读一遍核对。
超时或异常的，停在「不知道」——不自动重发，也不会告诉你「成功了」。

## 几条不让步的规矩

改了哪条，就等于把这个项目的目的改掉了。

| 规矩 | 怎么看出来没被破坏 |
|---|---|
| 权限只能收窄 | 判断只在 `ToolGateway` 一处；审批单冻结边界快照，执行时取「快照 ∩ 当前」，放宽不生效 |
| 写操作必须人确认 | 没有自动批准，没有「按风险免审」的开关 |
| 不知道就说不知道 | `EXECUTION_UNKNOWN` 是终点，不是重试的起点 |
| 先过滤权限再检索 | 无权看的切片进不了候选集，也进不了 prompt |
| 租户隔离三层 | 身份解析 → ORM 过滤 → PostgreSQL RLS；跨租户只有一条显式路径，且必写审计 |
| 工具只能从注册表来 | 没登记的工具对模型不可见；远端 MCP 工具必须逐条登记 |

## 现在能做什么

- **聊**：普通问答、只读工具直调、知识问答带引用、写操作先出预览。
- **知识库**：Markdown 入库，增量更新，权限内检索，答复带编号引用。
- **多个 agent 协作**：可以委派、可以传引用（产物 / 消息 / 文档 / 业务对象），
  被委派方要写东西时自己开审批单，单子上冻结整条委派链。
- **工具接入**：内置工具、远程 MCP、自定义 HTTP Tool（内网和云元数据地址直接拒）、
  从 OpenAPI 文档导入。
- **连接器**：外部系统的地址和认证只写一次，凭据按租户各存一份；缺凭据就不调，
  不会偷偷退回 `.env`。
- **编排**：说一段话起草一张流程图，起草不落库；改一个节点不用重画整张；保存前自动备份。
- **治理**：三态工具策略（允许 / 要确认 / 拒绝）、风险只上调、四个写动作全审计。
- **部署**：单机 systemd + Nginx，一条 `deploy/sync.sh` 推上去并核对服务器已对齐。

## 刻意不做的

写在这里，不是忘了做：

- **不做向量检索**。现在是 BM25 + 大模型语义重筛。重筛只能剔除误召回，救不回一条没召回的。
  接口按「先过滤后检索」留好了，将来换实现不影响调用方。
- **不做自动批准**，任何形式都不做。
- **不做连接器的 OAuth2 / 每用户授权 / 凭据加密存储**，也不做连接器级限流。
- **不做** Langfuse 上报、RBAC 管理接口。
- **Checkpointer 默认 SQLite**，多实例自己换 PostgresSaver。
- **场站类工具在没配后端地址时走 mock**，返回值带 `source=mock`，别当真数据用。

## 该不该用

- 适合：想让智能体真的去操作系统，但接受不了「模型自己决定就执行」的团队；
  要自托管、数据不出自己库的组织。
- 不适合：想一个周末拼个 demo 的；不打算维护 PostgreSQL 和迁移的；
  期待开箱即用 SaaS 的那套 OAuth 的。

## 从哪读起

| 想知道 | 读 |
|---|---|
| 有哪些层、一次对话怎么走、为什么审批要跨两次请求 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| agent 的职责边界和治理规矩 | [`docs/AGENTS.md`](docs/AGENTS.md) |
| 委派、传引用、配额怎么算 | [`docs/A2A.md`](docs/A2A.md) |
| 三类工具、HTTP Tool、OpenAPI 导入 | [`docs/TOOLS.md`](docs/TOOLS.md) |
| 三态策略、风险只上调 | [`docs/PERMISSIONS.md`](docs/PERMISSIONS.md) |
| 连接器和凭据怎么发 | [`docs/CONNECTORS.md`](docs/CONNECTORS.md) |
| 入库、切片、检索、评测 | [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md) |
| 说一段话建流程图 | [`docs/WORKFLOW_AUTHORING.md`](docs/WORKFLOW_AUTHORING.md) |
| 从文件变成知识库 / 技能 / 工具 | [`docs/STUDIO_IMPORT.md`](docs/STUDIO_IMPORT.md) |
| 多租户和 RLS | [`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md) |
| 单机部署、怎么发版 | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| 代码沙箱，什么时候该升级 | [`docs/SANDBOX.md`](docs/SANDBOX.md) |

## In English

Most agent demos are a long system prompt plus a handful of tools. They look great —
until someone asks who approved that write, or which agent produced this answer two weeks ago.

PAP is a self-hosted platform for running several agents inside one organization without
losing track of either question. Every agent is a row in the database: a name, a job,
a tool boundary, an on/off switch. Permissions only narrow along the delegation chain,
never widen — the effective scope is always *the human's permissions ∩ every agent boundary
on the path*. Every write goes preview → a human clicks confirm → execute → read back and
verify. If an external call times out, the operation stops at `EXECUTION_UNKNOWN` instead of
being retried behind your back.

Retrieval filters by permission before scoring, so chunks you may not read never reach the
prompt. Tenancy is enforced three times over: identity, ORM, PostgreSQL row-level security.

No vector store, no auto-approve, no SaaS control plane. Start with
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the short version of what works today is above.
