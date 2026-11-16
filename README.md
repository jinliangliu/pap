# Personal Agent Platform

多智能体平台：把「多个聪明 Agent 的偶然协作」做成**有身份、有分工、有治理、可沉淀、
可持续运营**的组织级基础设施。技术栈是 FastAPI + LangGraph + 受控工具网关 +
写操作审批 + 可追溯知识检索。

「智能体」在这里是**可运营的对象**而不是代码里的一个函数：有名字、有职责、
有工具边界、有生命周期，在控制台上增删改，边界只在工具网关上收窄、绝不放宽。
设计口径与暂不做的部分见 [`docs/AGENTS.md`](docs/AGENTS.md)。

本仓库对应《Personal Agent Platform：第 1～11 批实施方案》的落地实现。

## 快速开始

```bash
uv sync                                  # 安装依赖
cp .env.example .env                     # 填写 OPENAI_API_KEY 等
uv run alembic upgrade head              # 建表（唯一的 schema 来源）
uv run python -m scripts.seed_dev_data   # 开发身份 + 站点授权（仅 development）
uv run python -m scripts.ingest_knowledge  # 可选：重建知识库索引
uv run python -m scripts.eval_knowledge    # 可选：检索评测基线（25 条标注集）
uv run uvicorn app.main:app --reload
```

检查：

```bash
curl -s localhost:8000/health
curl -s localhost:8000/ready
uv run ruff check .
uv run pytest
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 进程存活 |
| GET | `/ready` | 依赖可用性（数据库、checkpointer、工具注册表、模型配置） |
| GET | `/metrics` | Prometheus 文本指标 |
| POST | `/api/v1/chat` | 对话（普通问答 / 只读工具 / 知识问答 / 写操作预览） |
| GET | `/api/v1/approvals/{id}` | 查询待确认操作 |
| POST | `/api/v1/approvals/{id}/confirm` | 确认并执行写操作 |
| POST | `/api/v1/approvals/{id}/cancel` | 取消写操作 |
| GET | `/api/v1/tools` | 当前身份可见的受控工具（含 `source`：builtin / mcp / custom） |
| GET | `/api/v1/conversations` | 当前身份的会话历史（按最近活跃倒序） |
| GET | `/api/v1/conversations/{id}` | 会话详情与全部消息 |
| PATCH | `/api/v1/conversations/{id}` | 重命名会话 |
| DELETE | `/api/v1/conversations/{id}` | 删除会话与消息 |
| GET | `/api/v1/knowledge/status` | 索引状态与可访问的数据分级 |
| POST | `/api/v1/knowledge/search` | 知识检索（返回可追溯引用） |
| GET | `/api/v1/me` | 当前身份、租户与可切换租户 |
| `*` | `/api/v1/agents*` | 智能体：清单、详情、新建、修改边界、启停、删除 |
| `*` | `/api/v1/skills*` | 技能：列表、详情、新建、修改、删除、导入导出、召回测试 |
| PUT/GET | `/api/v1/artifacts` | 上传 / 列出附件（按租户 + 用户隔离） |
| GET | `/api/v1/artifacts/{id}/content` | 下载附件（强制 attachment） |
| `*` | `/api/v1/admin/*` | 平台管理通道：租户、成员、配额、工具策略、审计 |
| `*` | `/api/v1/admin/tools*` | 自定义 HTTP Tool：列表、新建、更新、删除、导入、导出、URL/curl 解析 |

身份：开发环境用 `X-Authenticated-Subject` 指定主体（缺省取 `DEV_SUBJECT`）；
对外服务必须设置 `AUTH_MODE=gateway`（可信网关注入）或 `AUTH_MODE=jwt`（本服务验签），
且不会为陌生主体自动建号。`APP_ENV=production` 下若仍是 `dev` 模式，应用拒绝启动。

多租户：`X-Authenticated-Tenant` 只是「切换到哪个组织」的**申请**，服务端一定会校验成员关系
（不属于则 403）。隔离由 ORM 自动过滤 + PostgreSQL RLS 双层落实，细节见
[`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md)。

## 部署

- 单机部署（systemd + Nginx 反代 + Basic 认证）：[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- LangGraph Studio 观测（SSH 隧道 + `baseUrl=http://127.0.0.1:2024`）：[`docs/STUDIO.md`](docs/STUDIO.md)
- 多租户隔离模型与运维：[`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md)
- 代码沙箱：[`docs/SANDBOX.md`](docs/SANDBOX.md)
- 工具来源、自定义 HTTP Tool 与导入导出：[`docs/TOOLS.md`](docs/TOOLS.md)
- 权限：三态工具策略（允许 / 需确认 / 拒绝）、风险只上调与两个平台开关：[`docs/PERMISSIONS.md`](docs/PERMISSIONS.md)
- 控制台「从文件新建」：一份文件按内容建成知识库文档 / 技能 / OpenAPI 工具：[`docs/STUDIO_IMPORT.md`](docs/STUDIO_IMPORT.md)
- 知识库：文档入库、增量同步、切片与检索：[`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md)
- 智能体：职责边界、生命周期与治理不变量：[`docs/AGENTS.md`](docs/AGENTS.md)
- 技能：作业指令、触发匹配与工具白名单：[`docs/SKILLS.md`](docs/SKILLS.md)
- 对话会话历史与删除语义：[`docs/CONVERSATIONS.md`](docs/CONVERSATIONS.md)
- 部署资产：[`deploy/systemd/`](deploy/systemd)、[`deploy/nginx/`](deploy/nginx)、[`deploy/firewall/`](deploy/firewall)

```bash
bash deploy/install.sh          # 安装 systemd 单元与 Nginx 站点（需 root）

# 发版：推送的是已提交的代码，未提交的改动不会上线
deploy/sync.sh --dry-run                # 说明将上线哪个提交
deploy/sync.sh                          # 推送并校验服务器已对齐
deploy/sync.sh --restart                # 推送 + 重启 + 健康检查
deploy/sync.sh --migrate                # 推送 + 停服迁移 + 重启 + 健康检查
deploy/sync.sh --verify                 # 只比对本地提交与服务器工作区
```

## 架构要点

完整分层与六张图见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
下面是几条最容易踩错的。

- **单一配置入口**：`app/core/config.py`。
- **单一 ORM**：`app/models/`，Alembic 迁移是唯一 schema 来源。
- **单一执行图**：`app/agents/graph.py`，节点在 `app/agents/nodes.py`。
- **工具只能从注册表调用**：`app/tools/registry.py` + `app/tools/gateway.py`，
  未注册工具对模型不可见。
- **智能体是一等公民**：`agents` 表定义「谁在做事、能碰什么」，
  有效权限恒为「人的权限 ∩ Agent 边界」，判断只在 `ToolGateway.agent_denial()`
  一处实现，目录过滤与执行拦截共用；不传 agent 等于平台基线，行为与改造前逐字一致。
  审批单冻结边界快照，执行时取「快照 ∩ 当前」——收窄立即生效，放宽不生效。
- **写操作强制审批**：先预览 → `interrupt` 等待确认 → 校验状态/归属/有效期/参数哈希
  → 执行（携带幂等键）→ 回读核验 → 审计。
- **不确定即未知**：外部调用超时或异常进入 `EXECUTION_UNKNOWN` /
  `RECOVERY_REQUIRED`，不自动重发，也不对用户表述为成功。
- **检索先过滤权限**：`app/knowledge/retriever.py` 在打分前按 `access_scope` 与切片 ACL 过滤。
- **租户隔离三层**：可信身份解析租户 → ORM 自动过滤/写入校验 → PostgreSQL RLS 兜底；
  跨租户只有 `platform_admin_tenant_scope` 一条显式路径，且必写审计。
- **远端 MCP 受控接入**：外部 MCP Server 的工具必须逐条登记在 `app/tools/mcp_tools.py`
  才会暴露给模型，未登记的远端工具永远不会进入模型目录；凭据按租户分发、未配置即拒绝。
  授权边界（能看到哪些站、能不能写）由 MCP 侧令牌决定，见 `docs/MCP_INTEGRATION.md`。
- **连接器（Connector）**：一个外部系统的端点与认证方式只写一次（平台级 `connectors`），
  凭据按租户各存一份（`connector_credentials`，挂 RLS），工具只记 `connector_id`。
  凭据缺失 fail-closed、绝不回退到 `.env`；连接器停用则其工具一起从目录消失。
  智能体边界可写连接器名（与 `allowed_tools` 并集），不必再列二十个工具名。
  见 [`docs/CONNECTORS.md`](docs/CONNECTORS.md)。

## 已知边界

- Checkpointer 默认 SQLite，适合单机；多实例请替换 `app/db/checkpoint.py` 为 PostgresSaver。
- 检索仍是 BM25（jieba 分词）+ chat 模型语义重筛，未接入向量库；接口已按“先过滤后检索”设计，替换实现不影响调用方。重筛只剔除已召回的假阳性，救不回零召回；标题字段加权与登记式同义词表只缓解局部漏召回。25 条 golden set 与评测脚本见 `scripts/eval_knowledge.py`，配置与基线数字见 `docs/KNOWLEDGE.md` §5。
- 场站相关工具在未配置 `STATION_PLATFORM_BASE_URL` 时进入显式 mock 模式，返回值带 `source=mock`，
  不得当作真实外部数据。
- 限流默认进程内计数；配置 `REDIS_URL` 后切换为 Redis 计数。
- 未实现：Langfuse 上报、RBAC 管理接口。
- 智能体之间已支持**委派 + 引用传递 + 写操作委派**（A2A P0 / P1 / P2）：`delegate_to_agent`
  可以带 `refs`（产物 / 会话消息 / 知识文档 / 业务对象），可见性在**被委派方**的上下文里
  重判，「无权看」「已失效」「没有材料」是三个不同错误；没有任何依据的结论不会过桥。
  被委派方执行写操作时，审批单由它自己开、由人确认，单子上冻结整条委派链——
  批准期间上游被收窄，这张单子就作废。Agent 级配额与绩效看板的口径写在
  [`docs/A2A.md`](docs/A2A.md)、[`docs/AGENTS.md`](docs/AGENTS.md) §8，不是遗漏。
- 连接器**没有** OAuth2 / 每用户授权、连接器级限流、定时巡检与凭据加密存储。
