# About PAP

> **一句话**：自托管的多智能体平台。用连接器把内网办公系统和外网智能体接到一起，
> 顺手搭起自己的知识库和工作流。
>
> **In one line**: A self-hosted multi-agent platform — connectors for the office systems
> inside your network and the agents outside it, plus a knowledge base and workflows of your own.

PAP（Personal Agent Platform）是一个你可以整个搬到内网、自己改、自己跑的智能体平台。
它不做成 SaaS：代码在你自己机器上，数据在你自己库里，哪个环节不合用就直接改。

FastAPI + LangGraph + PostgreSQL，单机 systemd + Nginx 就能起。

## 能拿它干什么

**连内网系统。** 连接器把地址和认证只写一次，凭据按租户各存一份。OA、工单、台账这类
只有内网能访问的系统，接上就能让智能体去查、去办。自定义 HTTP 工具支持从 OpenAPI 文档导入；
内网地址是**故意放行**的（不然接不了内网系统），只拦链路本地和云元数据地址。

**连外网智能体。** 远端 MCP Server 的工具逐条登记后才会出现在模型目录里；
智能体之间可以互相委派，带产物、消息、文档、业务对象做引用传递。

**自建知识库。** 把手上的 Markdown 扔进去就能检索，答复带编号引用。
切片权限按人分配，没有权限的切片连召回候选都进不去。

**搭工作流。** 说一段话起草一张流程图，起草阶段不落库；改一个节点不用重画整张；
保存前自动备份，画错了能回滚。

**自己动手改。** 智能体是数据库里的行，不是代码里的函数——名字、职责、能用哪些工具、
开不开，全在控制台上改。加一个工具、换一个模型、拆一遍编排，都不用重新发版。

## 有一条规矩不让步

模型可以动手，但**写操作必须有人点确认**。流程是：先给预览 → 人确认 → 执行 → 再读一遍核对。
外部调用超时或异常就停在「不知道」，不自动重发，也不会告诉你「成功了」。
审批单会冻结当时的权限快照，之后谁被收窄都立即生效，谁被放宽都不生效。

这条不改。改了，这个项目就不是同一个东西了。

## 适合谁

- 想在自己机器或内网里跑智能体，数据不出自己库的。
- 手上有一堆内网系统想接进来，又不愿意为此买一套平台的。
- 愿意读代码、改代码，把平台调成自己形状的。

**不适合**：想开箱即用的；不打算维护 PostgreSQL 和迁移的；指望它替你做产品决策的。

## 明确不做的

- **不做向量检索**。现在是 BM25 + 大模型语义重筛。重筛只能剔除误召回，救不回没召回的。
  接口按「先过滤后检索」留好了，将来换实现不影响调用方。
- **不做自动批准**，任何形式都不做。
- **不做 SaaS 控制台**，也不做连接器的 OAuth2 / 每用户授权 / 凭据加密存储。
- **Checkpointer 默认 SQLite**，多实例自己换 PostgresSaver。
- **场站类工具在没配后端地址时走 mock**，返回值带 `source=mock`，别当真数据用。

## 从哪读起

| 想知道 | 读 |
|---|---|
| 有哪些层、一次对话怎么走 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 怎么接内网系统、凭据怎么管 | [`docs/CONNECTORS.md`](docs/CONNECTORS.md) |
| 三类工具、HTTP Tool、OpenAPI 导入 | [`docs/TOOLS.md`](docs/TOOLS.md) |
| 智能体怎么分工、边界怎么收 | [`docs/AGENTS.md`](docs/AGENTS.md) |
| 委派、传引用、配额怎么算 | [`docs/A2A.md`](docs/A2A.md) |
| 知识库入库、切片、检索、评测 | [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md) |
| 说一段话建工作流 | [`docs/WORKFLOW_AUTHORING.md`](docs/WORKFLOW_AUTHORING.md) |
| 从文件变成知识库 / 技能 / 工具 | [`docs/STUDIO_IMPORT.md`](docs/STUDIO_IMPORT.md) |
| 多租户和权限 | [`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md)、[`docs/PERMISSIONS.md`](docs/PERMISSIONS.md) |
| 单机部署、怎么发版 | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |

## In English

PAP is a self-hosted multi-agent platform you can drop inside your own network and change
as you like. Connectors reach the office systems that only exist on your intranet — one place
for the endpoint, one credential per tenant — and registered MCP servers bring in agents from
outside. Agents are database rows rather than code: name, job, tool boundary, on/off switch,
all editable from the console without a redeploy.

It also gives you a knowledge base (Markdown in, citations out, permission filtering before
scoring) and a workflow builder (describe it in a sentence, it drafts a graph; edit one node
without redrawing the rest; a backup is taken before every save).

One rule does not bend: writes need a human to confirm. Preview → confirm → execute →
read back and verify. Timeouts stop at `EXECUTION_UNKNOWN` instead of being retried silently.

Stack: FastAPI + LangGraph + PostgreSQL. Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
