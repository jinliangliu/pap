# Personal Agent Platform：第 1～7 批实施方案

> 本文覆盖个人/业务 Agent 平台的基础建设阶段。  
> 后续的审批恢复、LangGraph 执行图、真实工具接入、RBAC、RAG 与评测体系，见《第 8～11 批实施方案汇总》。

---

# 第 1 批：项目骨架、开发环境与最小 API

## 目标

建立可复现的本地开发环境和可运行的 FastAPI 服务骨架，为后续 Agent Runtime、数据库、工具和前端接入提供稳定基础。

## 推荐技术选型

| 类别 | 建议 |
|---|---|
| Python | Python 3.11 |
| 包管理与虚拟环境 | `uv` |
| Web API | FastAPI + Uvicorn |
| 数据校验 | Pydantic v2 + pydantic-settings |
| 数据库 ORM | SQLAlchemy 2.x |
| 数据库迁移 | Alembic |
| 初期数据库 | SQLite |
| 生产数据库 | PostgreSQL |
| 代码质量 | Ruff、Pytest、Pyright 或 MyPy |
| 容器 | Docker Compose |

## 初始依赖

```bash
uv add fastapi "uvicorn[standard]" \
  pydantic pydantic-settings \
  sqlalchemy alembic \
  python-dotenv httpx

uv add --dev pytest pytest-asyncio ruff
推荐目录结构
personal-agent-platform/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── router.py
│   │   └── routes/
│   │       ├── health.py
│   │       └── chat.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── exceptions.py
│   ├── schemas/
│   │   └── chat.py
│   └── services/
│       └── chat_service.py
├── tests/
│   ├── unit/
│   └── integration/
├── data/
│   ├── knowledge/
│   ├── uploads/
│   └── exports/
├── docs/
├── alembic/
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
配置管理
.env.example 示例：

APP_ENV=development
APP_NAME=personal-agent-platform
APP_HOST=127.0.0.1
APP_PORT=8000
LOG_LEVEL=INFO

DATABASE_URL=sqlite:///./data/app.db

OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=
要求：

.env 不提交 Git；
不在源码、日志、测试快照中写入真实密钥；
配置项由 pydantic-settings 统一读取；
在启动阶段校验关键环境变量；
开发、测试、生产使用不同配置来源。
最小 API
GET  /health
GET  /ready
POST /api/v1/chat
建议响应语义：

{
  "status": "ok",
  "service": "personal-agent-platform",
  "version": "0.1.0"
}
/health 只说明 Web 服务进程存活； /ready 应检查数据库、模型配置等关键依赖是否可用。

验收标准
uv run uvicorn app.main:app --reload 可正常启动。
GET /health 返回服务存活状态。
GET /ready 可以区分“进程存活”和“依赖可用”。
POST /api/v1/chat 可接收请求并返回固定测试响应。
.env、虚拟环境、数据库文件和临时导出目录均被 .gitignore 排除。
基础单元测试可通过。
Ruff 检查通过。
第 2 批：数据库、会话、用户与基础审计
目标
建立平台的持久化基础：用户、会话、消息、运行记录与审计日志，为后续记忆、审批、工具调用和权限控制提供数据底座。

数据库演进策略
开发阶段：SQLite
      ↓
多人使用、并发任务、审批与生产部署：
PostgreSQL
SQLite 适用于单机快速开发；以下场景应尽快迁移 PostgreSQL：

多用户；
并发会话；
审批和任务恢复；
站点或资源授权；
向量检索；
审计查询；
多实例部署。
初始数据表建议
users
conversations
messages
agent_runs
audit_logs
users
id
external_subject
display_name
status
created_at
updated_at
说明：

external_subject 用于未来对接 OIDC、企业 SSO、微信或其他身份系统；
初期可以使用本地开发用户，但不要把“开发用户 ID”当成生产认证方案。
conversations
id
user_id
title
channel
created_at
updated_at
last_active_at
messages
id
conversation_id
role
content
content_type
created_at
metadata_json
角色可包括：

system
user
assistant
tool
agent_runs
id
conversation_id
user_id
status
input_summary
output_summary
started_at
finished_at
error_category
运行状态建议：

PENDING
RUNNING
WAITING_FOR_APPROVAL
COMPLETED
FAILED
CANCELLED
audit_logs
id
actor_user_id
action
resource_type
resource_id
result
created_at
metadata_json
Alembic 迁移要求
所有结构变更均通过 Alembic：

uv run alembic revision --autogenerate -m "create initial tables"
uv run alembic upgrade head
禁止：

生产环境手工修改表结构；
仅修改 ORM Model 却不生成迁移；
用临时 SQL 绕过迁移记录。
基础审计原则
审计日志记录“发生了什么”，但不存敏感明文。

建议记录：

用户标识
动作名称
对象类型
对象 ID
结果
时间
请求关联 ID
脱敏参数摘要
禁止记录：

密码
Token
Cookie
Authorization Header
私钥
完整敏感业务内容
验收标准
数据库 Schema 由 Alembic 管理。
可以创建用户、会话、消息和运行记录。
新建会话与历史消息可在服务重启后读取。
Agent Run 与会话、用户可关联查询。
关键 API 操作产生基础审计日志。
测试环境可使用独立数据库，避免污染开发数据。
第 3 批：模型接入与最小对话能力
目标
接入一个 LLM 服务，实现带会话历史的最小对话能力，同时建立模型失败、超时、无效响应时的安全处理方式。

支持的模型适配方式
推荐优先支持 OpenAI 兼容接口：

OpenAI
DeepSeek
Qwen
Ollama OpenAI-compatible endpoint
其他兼容 API 服务
后续可按需要补充：

Anthropic Claude
Azure OpenAI
Gemini
本地 Ollama 原生适配
模型配置字段
provider_name
protocol
base_url
model
temperature
timeout_seconds
max_retries
enabled
注意：

API Key 只从环境变量或 Secret Manager 读取；
不记录到数据库、审计日志、Trace 或模型上下文；
模型配置与密钥分离管理；
为不同模型设置独立超时、重试、上下文窗口与成本策略。
对话请求结构
{
  "conversation_id": "optional-existing-conversation-id",
  "message": "帮我总结今天的待办",
  "stream": false
}
响应示例：

{
  "conversation_id": "conv_xxx",
  "run_id": "run_xxx",
  "status": "completed",
  "answer": "……"
}
对话处理流程
接收用户消息
  ↓
识别用户与会话
  ↓
保存用户消息
  ↓
加载有限历史上下文
  ↓
调用模型
  ↓
保存助手回复
  ↓
记录 Agent Run
  ↓
返回响应
上下文控制原则
不要无限拼接历史消息。建议：

最近 N 轮完整消息
+ 会话摘要
+ 当前请求
+ 已授权的必要上下文
当上下文过长时：

保留最近对话；
对旧对话生成结构化摘要；
明确区分“已确认事实”“用户偏好”“未验证推断”；
不把敏感原文自动写入长期记忆。
模型异常处理
应区分：

模型认证失败
模型服务超时
模型限流
网络异常
返回格式无效
内容被安全策略拦截
模型不可用
对用户的表达应简洁，例如：

当前模型服务暂时不可用，请稍后重试。

不得伪造模型已经完成回答。

验收标准
可通过配置切换模型服务和模型 ID。
一次对话可保存用户输入和模型输出。
同一会话可加载必要历史消息。
模型超时、限流和认证失败会被准确区分并记录。
模型失败时不会伪造成功结果。
日志、数据库和错误响应中不包含 API Key。
第 4 批：LangGraph 最小状态机与会话恢复
目标
从线性“请求 → 模型回复”升级为最小可追踪状态机，为后续工具调用、检索、审批中断和恢复做准备。

初始执行图
START
  ↓
load_context
  ↓
classify_request
  ↓
respond
  ↓
persist_state
  ↓
END
初期不要急于引入复杂多 Agent、自动递归规划或大量并行分支。

建议节点职责
load_context
负责：

加载用户身份
加载会话
加载最近消息
加载会话摘要
加载必要配置
classify_request
初步将请求分为：

chat
knowledge
tool_query
tool_write
分类结果只是流程选择依据，不应直接授予权限或跳过工具校验。

respond
负责：

普通对话回答
模型调用
输出安全检查
保存结果
persist_state
负责：

保存运行状态
写入检查点
写入运行摘要
更新会话活跃时间
Checkpoint 策略
开发阶段可用 SQLite Checkpoint；生产建议使用 PostgreSQL。

检查点至少应支持：

thread_id
graph_state
current_node
run_id
created_at
updated_at
要求：

同一会话重启后可恢复；
不把密钥、完整 Token、未授权资料写入状态；
状态更新具备版本或锁，避免并发覆盖；
中断后的恢复必须校验当前用户和会话关联。
错误路径
节点异常
  ↓
记录错误分类
  ↓
保存当前状态
  ↓
返回安全且真实的失败提示
不要把异常吞掉后继续假装执行完成。

验收标准
每次请求有唯一 Run 标识。
可查看一次运行经过的节点。
会话状态可被持久化。
服务重启后可恢复会话上下文。
节点失败可记录失败原因与执行位置。
普通聊天仍能稳定完成，不因引入图框架而退化。
第 5 批：基础 Tool Registry 与只读工具能力
目标
建立“模型不能直接访问外部系统”的工具边界。先引入可控的只读工具，再考虑写操作和审批。

基本原则
模型提出调用意图
≠
模型拥有工具权限
≠
工具可以绕过系统校验
工具必须显式注册，模型只能看到被允许暴露的本地工具描述。

工具注册表字段
tool_name
display_name
description
input_schema
output_schema
risk_level
enabled
required_permission
handler
timeout_seconds
风险等级建议：

read
normal
write
execute
destructive
external
第 5 批仅建议开放：

read
首批工具建议
优先选择低风险、只读、可验证工具：

搜索个人笔记
查询日历事件
查询待办事项
查询天气
查询 GitHub Issue
读取授权目录中的文档
查询业务系统状态
不建议在本批开放：

发送邮件
创建日历事件
删除文件
提交代码
推送代码
修改云资源
执行 Shell 命令
控制设备
Tool Gateway 最小流程
模型请求工具
  ↓
检查工具是否注册
  ↓
检查工具是否启用
  ↓
校验 JSON Schema
  ↓
检查风险等级
  ↓
调用 Handler
  ↓
标准化结果
  ↓
记录运行与审计信息
  ↓
交给模型组织最终回复
标准化工具结果
不要让模型直接依赖任意格式的外部响应。

建议统一结构：

{
  "success": true,
  "status": "ok",
  "data": {},
  "message": "查询成功",
  "verification": {
    "verified": true
  },
  "metadata": {
    "tool_name": "query_calendar_events",
    "duration_ms": 138
  }
}
失败时：

{
  "success": false,
  "status": "external_error",
  "data": null,
  "message": "外部服务暂时不可用",
  "verification": {
    "verified": false
  }
}
Tool 输出使用规则
模型需要遵循：

工具调用返回成功，不必然代表业务成功；
外部系统返回空数据，不等于资源不存在；
超时不等于操作失败，也不等于操作成功；
数据缺失时必须保留未知项；
不将工具错误伪装成正常业务结论。
验收标准
工具只能从注册表中调用。
未注册工具无法被模型调用。
工具参数经过 JSON Schema 校验。
工具调用有超时控制。
可执行至少 2 个真实只读工具。
工具业务失败不会被表述为“已完成”。
每次工具调用记录名称、耗时、状态和参数摘要。
第 6 批：知识库、文档导入与基础 RAG
目标
引入可追溯的知识检索能力，使 Agent 可根据个人资料、项目文档或业务资料回答问题，并能够说明依据来自哪里。

初期技术选型
本地快速开发可选：

Chroma
FAISS
SQLite + 向量扩展（视环境而定）
稳定运行、多用户、权限过滤场景建议：

PostgreSQL + pgvector
初始依赖建议
uv add chromadb langchain-chroma
uv add langchain-text-splitters
uv add pypdf python-docx markdown beautifulsoup4
若使用 PostgreSQL + pgvector：

uv add psycopg[binary] asyncpg pgvector langchain-postgres
文档目录建议
data/
└── knowledge/
    ├── projects/
    ├── notes/
    ├── manuals/
    ├── meetings/
    └── inbox/
入库流程
原始文件
  ↓
文件类型识别
  ↓
安全排除与大小限制
  ↓
文本抽取
  ↓
文档元数据生成
  ↓
文本分段
  ↓
向量化
  ↓
写入索引
  ↓
检索验证
文档元数据
每份文档至少保存：

{
  "document_id": "unique-document-id",
  "title": "文档标题",
  "source_path": "data/knowledge/projects/example.md",
  "document_type": "project_document",
  "tags": ["project", "architecture"],
  "updated_at": "2026-11-02T00:00:00+08:00",
  "content_hash": "sha256:...",
  "access_scope": "personal"
}
默认排除清单
.env
.env.*
*.pem
*.key
id_rsa
id_ed25519
credentials.json
token.json
cookies.txt
node_modules/
.venv/
.git/
__pycache__/
数据库备份
日志目录
构建产物
二进制文件
RAG 回答规范
回答需要区分：

资料明确说明：
根据资料推断：
当前资料不足以确认：
最小引用信息：

文档标题
章节或页码
更新时间
找不到足够资料时：

当前知识库中未找到足以支持该结论的资料。

禁止：

把模型常识说成知识库结论；
将未授权或未检索到的文档虚构为引用；
因为资料不完整而自行补齐业务事实。
验收标准
可导入 Markdown、TXT、PDF 和 Word 中至少两类文档。
每份文档具备来源、更新时间和哈希。
可按问题检索相关片段。
回答包含可追溯来源。
找不到资料时明确说明依据不足。
索引可从原始资料重建。
密钥、配置文件和敏感文件不会被自动入库。
第 7 批：Docker Compose、可观测性、测试与部署准备
目标
使应用具备可重复部署、基础可观测性和自动化回归能力，为第 8 批开始的审批、幂等与高风险操作打牢运行基础。

Docker Compose 服务建议
初期可从以下服务开始：

app
postgres
redis（可选）
后续按需加入：

langfuse
minio
reverse-proxy
worker
示例结构：

services:
  app:
    build: .
    env_file:
      - .env
    depends_on:
      - postgres
    ports:
      - "127.0.0.1:8000:8000"

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agent
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: change_me
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"

  redis:
    image: redis:7-alpine
    ports:
      - "127.0.0.1:6379:6379"

volumes:
  postgres_data:
生产注意事项：

数据库、Redis 不应默认暴露到公网；
密码不写入 Git；
使用独立的生产环境变量或 Secret Manager；
数据卷应有备份策略；
应用运行用户不应使用 root。
日志规范
日志建议采用 JSON 或可结构化解析的格式，至少包含：

timestamp
level
service
request_id
conversation_id
run_id
user_id（可脱敏）
event
duration_ms
error_category
禁止记录：

API Key
Token
Cookie
Authorization
密码
私钥
完整敏感文档
基础指标
建议至少统计：

API 请求数
API 错误率
响应耗时
模型调用耗时
模型调用失败率
工具调用次数
工具调用失败率
数据库连接失败
运行状态分布
Trace 建议
最小追踪链路：

HTTP Request
→ Agent Run
→ Model Call
→ Tool Call
→ Database Operation
→ HTTP Response
初期可使用 OpenTelemetry；模型与 Agent 可观测性可选择 Langfuse 或 LangSmith 之一。

测试层级
tests/
├── unit/
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_tool_registry.py
│   └── test_rag_chunking.py
├── integration/
│   ├── test_api_health.py
│   ├── test_chat_api.py
│   ├── test_database.py
│   └── test_tool_calls.py
├── graph/
│   └── test_basic_agent_graph.py
└── evals/
    ├── basic_chat.yaml
    ├── knowledge_qa.yaml
    └── tool_query.yaml
最小 CI 检查
每次提交建议执行：

代码格式检查
→ 静态检查
→ 单元测试
→ 集成测试
→ 数据库迁移校验
→ 基础 Agent 回归测试
例如：

uv run ruff check .
uv run pytest
uv run alembic upgrade head
上线前运行检查
/health 正常
/ready 正常
数据库迁移已执行
模型配置已校验
关键环境变量已配置
日志正常输出
工具注册表已加载
测试用户与生产用户隔离
备份与恢复
至少应备份：

PostgreSQL 数据
上传的原始文件
知识库原始资料
关键配置模板
数据库迁移版本
建议定期验证：

数据库恢复是否成功
索引是否可从原始文件重建
备份文件是否真实可用
验收标准
可用 Docker Compose 一键启动开发或测试环境。
PostgreSQL 数据持久化到数据卷。
日志包含请求与运行关联标识。
关键错误可通过日志和 Trace 定位。
基础单元测试与集成测试可自动执行。
数据库迁移可在空数据库与已有数据库上正确执行。
应用、数据库和模型依赖具备健康检查。
数据库与原始知识资料具备备份和恢复验证方案。
第 1～7 批完成后的能力边界
完成第 1～7 批后，平台应已经具备：

可运行的 FastAPI 服务
+ 用户、会话、消息和运行记录持久化
+ 可配置的大模型对话
+ 基础 LangGraph 状态机
+ 受控的只读工具调用
+ 可追溯的知识库问答
+ 容器化部署基础
+ 日志、指标、Trace 与测试基础
但此时仍不应默认开放：

外部写操作
发送邮件
创建日历
修改业务数据
控制设备
执行本机命令
自动重试外部写请求
跨站点或跨用户资源访问
这些能力应在第 8 批及后续批次中，通过审批、幂等、RBAC、资源授权、回读核验与审计机制逐步开放。

# Personal Agent Platform：第 8～11 批实施方案汇总

> 目标：在已有 FastAPI、LangGraph、数据库与基础工具层的基础上，逐步建设一个可审计、可授权、可恢复、可观测的个人/业务 Agent 平台。

---

## 总体架构

```text
用户 / Web / API / 聊天通道
        ↓
认证网关 / OIDC
        ↓
FastAPI API 层
        ↓
LangGraph Agent Runtime
  ├─ 请求理解与路由
  ├─ 知识检索与引用
  ├─ 工具计划与调用
  ├─ 审批中断与恢复
  ├─ 长期记忆
  └─ 输出校验
        ↓
Tool Gateway
  ├─ 工具白名单
  ├─ JSON Schema 校验
  ├─ RBAC 与站点授权
  ├─ 风险与审批校验
  ├─ 限流、锁与幂等
  ├─ HTTP / MCP Adapter
  ├─ 回读核验
  └─ 审计与追踪
        ↓
业务 HTTP API / Remote MCP / 知识库 / 数据库
核心原则：

模型负责理解、规划与组织回复，不直接拥有系统权限。
工具必须显式注册，远端新增能力不能自动暴露。
写操作必须遵循：预览 → 单次审批 → 执行 → 回读核验 → 审计。
外部写入超时或连接中断时，状态应为未知，不得自动重发。
所有资源访问均需同时满足：身份认证、角色权限、资源/站点授权。
所有答案、工具结果和知识检索均应区分“已证实事实”“推断”“未知”。
第 8 批：审批、任务状态、幂等与恢复
目标
建立高风险操作的可靠执行机制，特别用于设备控制、日历写入、文件修改、邮件发送和外部系统变更。

标准流程
用户提出写操作
  ↓
参数解析与校验
  ↓
生成预览（不执行）
  ↓
保存审批请求
  ↓
等待用户单次确认
  ↓
确认后再次校验状态、参数和有效期
  ↓
执行外部写操作（携带幂等键）
  ↓
回读外部系统
  ↓
记录最终状态、审计记录和用户可见结果
关键状态
建议使用以下审批/执行状态：

PENDING
APPROVED
REJECTED
EXPIRED
EXECUTING
EXECUTED
VERIFIED
FAILED
RECOVERY_REQUIRED
EXECUTION_UNKNOWN
状态含义：

PENDING：已生成预览，等待用户确认。
APPROVED：用户确认，但尚未开始外部执行。
EXECUTING：正在调用外部系统。
VERIFIED：执行完成且已通过外部回读核验。
FAILED：确认未执行或外部系统明确返回失败。
RECOVERY_REQUIRED：执行过程发生异常，需要根据幂等键回读恢复。
EXECUTION_UNKNOWN：无法确认外部系统是否已经执行，禁止自动重发。
审批数据建议
审批记录至少包含：

approval_id
thread_id
run_id
requested_by
tool_name
risk_level
parameters_json
parameter_hash
preview_json
idempotency_key
status
created_at
expires_at
approved_at
executed_at
verified_at
关键约束
审批有效期建议为 5 分钟。
审批绑定具体参数；参数变化后必须重新生成预览并重新确认。
不能复用历史审批、跨用户审批或跨会话审批。
同一审批只能恢复一次。
服务重启后仍能恢复待确认操作。
外部系统回读失败时，不得声称操作已成功。
自动重试仅适用于已证明幂等的只读请求；写操作默认不自动重试。
验收标准
未确认前绝不调用实际写工具。
确认后必须检查审批是否过期、参数是否一致、用户身份是否一致。
外部调用必须携带服务端生成的幂等键。
外部写超时后进入 RECOVERY_REQUIRED 或 EXECUTION_UNKNOWN。
回读成功才可以对用户表述为“已核验完成”。
相同请求不会因网络重试造成重复写入。
第 9 批：LangGraph 执行图、上下文与受控编排
目标
将 Agent 从普通聊天逻辑升级为可恢复、可追踪、可测试的状态机执行图。

推荐执行图
START
  ↓
load_request_context
  ↓
authenticate_and_resolve_principal
  ↓
classify_intent
  ├─ 普通对话 → respond
  ├─ 知识问答 → retrieve_knowledge → answer_with_citations
  ├─ 只读查询 → plan_tool_action → execute_tool → verify_result → respond
  └─ 写操作 → create_preview → interrupt_for_approval
                                         ↓
                                    resume_after_approval
                                         ↓
                                  execute_write_operation
                                         ↓
                                    verify_external_result
                                         ↓
                                           respond
  ↓
persist_state_and_trace
  ↓
END
建议状态字段
class AgentState(TypedDict):
    thread_id: str
    user_id: str
    user_message: str

    intent: str | None
    selected_skill: str | None

    messages: list
    retrieved_documents: list
    citations: list

    tool_plan: list
    tool_results: list

    approval_id: str | None
    execution_status: str | None

    final_answer: str | None
    errors: list[str]
上下文使用规则
用户请求、会话状态、用户身份和站点授权是运行时事实。
检索资料只能作为证据，不能覆盖系统权限或审批边界。
工具输出必须先验证业务结果，再写入最终回复。
模型不能把“工具调用成功返回”误表述为“业务动作已经成功”。
长期记忆只能保存已确认、必要且符合数据分级规则的信息。
不将 Token、Cookie、认证头、私钥、原始敏感文档写入模型上下文或长期记忆。
工具规划约束
模型产出的工具计划必须经过 Tool Gateway 校验：

工具是否注册
→ 工具是否启用
→ 参数是否符合 JSON Schema
→ 用户是否具备角色权限
→ 用户是否拥有目标站点/资源授权
→ 是否属于写操作
→ 是否需要审批
→ 是否满足限流、锁和幂等要求
→ 执行
→ 回读和结果核验
验收标准
每次运行拥有独立 thread_id 和可追踪状态。
服务重启后可以恢复等待审批的执行图。
只读、写入、知识问答和普通对话进入不同受控路径。
任何高风险写操作都必须中断并等待审批。
工具异常、业务失败、回读失败和状态未知可以明确区分。
图节点、工具调用、审批状态和最终输出可以关联追踪。
第 10 批：真实 HTTP/MCP 工具、RBAC、授权、限流与可观测性
目标
将示例工具替换为真实、受控的 HTTP/MCP 工具层，并加入身份、角色、站点级授权、速率限制、审计和 Trace。

模块结构建议
app/
├── auth/
│   ├── dependencies.py
│   ├── permissions.py
│   └── service.py
├── db/
│   ├── base.py
│   ├── models.py
│   └── session.py
├── integrations/
│   ├── http_client.py
│   ├── mcp_client.py
│   ├── result.py
│   └── station_platform.py
├── observability/
│   ├── logging.py
│   ├── metrics.py
│   └── tracing.py
├── services/
│   ├── authorization_service.py
│   ├── rate_limit_service.py
│   └── tool_execution_service.py
├── tools/
│   ├── base.py
│   ├── gateway.py
│   ├── registry.py
│   └── station_tools.py
├── alembic/
│   └── versions/
└── tests/
    ├── unit/
    └── integration/
1. 认证与身份来源
推荐由可信身份网关或 OIDC 完成身份认证。

客户端
  ↓
身份网关验证 JWT / SSO
  ↓
网关移除客户端伪造身份头
  ↓
网关注入已验证身份声明
  ↓
Agent API 接收可信身份
禁止：

直接信任公网客户端自定义的 X-User-ID 等身份头；
仅 Base64 解码 JWT 而不验证签名；
忽略 iss、aud、过期时间和撤销策略。
2. RBAC 与站点授权
建议数据模型：

users
roles
user_roles
role_permissions
station_grants
权限示例：

station.read
station.alarm.read
station.report.export
station.control.preview
station.control.execute
station.schedule.write
maintenance.ticket.create
audit.read
admin.manage_access
站点授权至少包含：

user_id
station_id
can_read
can_write
访问站点资源时必须同时满足：

用户已认证
+ 用户启用
+ 角色包含所需 permission
+ 用户被授予目标 station_id
+ 写操作要求 can_write
+ 高风险操作已经过单次审批
3. HTTP Tool Adapter
所有真实 HTTP 请求必须通过统一 Adapter，而不是散落在各 Tool Handler 中。

Adapter 负责：

固定 Base URL
认证注入
超时
连接错误处理
安全重试
响应标准化
业务错误分类
幂等键
脱敏日志
Trace
重试规则
请求类型	自动重试	规则
幂等只读 GET	可以	仅连接失败、读超时等暂态异常；限制次数
创建预览	谨慎	必须确认外部接口的幂等语义
真实写入	默认禁止	网络异常可能已被外部系统受理
可按幂等键查询的写入	先回读	不直接重发
写操作网络异常后的处理：

不要自动重发
→ 保存幂等键
→ 标记 RECOVERY_REQUIRED
→ 按幂等键查询外部系统
→ 根据外部证据更新 VERIFIED / FAILED / EXECUTION_UNKNOWN
4. Remote MCP Adapter
MCP 接入原则：

仅允许已配置的 Remote MCP 服务。
仅允许本地工具注册表明确暴露的工具。
模型不得指定任意 MCP URL。
远端发现的新工具不得自动暴露。
本地工具名和远端工具名应通过服务端静态映射。
MCP 工具同样经过 Schema、RBAC、资源授权、审批和审计。
本地工具：query_calendar_events
      ↓
服务端映射
      ↓
远端 MCP 工具：calendar_list_events
模型不应直接看到或选择远端原始工具名。

5. 工具注册元数据
每个注册工具建议声明：

name
display_name
description
input_schema
risk_level
requires_approval
required_permission
station_id_argument
handler
verifier
enabled
风险级别：

read
normal
write
execute
destructive
external
6. Tool Gateway
工具调用标准链路：

模型请求工具
  ↓
工具是否注册、是否启用
  ↓
JSON Schema 校验
  ↓
解析当前用户
  ↓
RBAC 权限校验
  ↓
站点/资源授权校验
  ↓
限流与业务锁
  ↓
审批校验
  ↓
调用 HTTP/MCP Handler
  ↓
标准化结果
  ↓
审计与 Trace
  ↓
必要时回读核验
对外错误应保持通用，例如：

当前请求未获授权。
当前资源不可访问。
操作请求过于频繁，请稍后再试。
外部系统暂时不可用。
执行状态暂时无法确认。
不得泄露：

其他站点是否存在
内部角色详情
Token、Cookie、认证 Header
数据库错误
服务器 URL、IP、堆栈
外部系统原始错误正文
7. 限流与并发保护
建议同时实现：

用户维度限流
工具维度限流
站点维度限流
API 网关 / IP 限流
全局配额保护
对于控制类操作，还应实现业务锁：

同一 station_id 同时只允许一个待执行或待核验控制操作。
业务锁不能代替审批、幂等键和外部回读。

8. 审计日志与脱敏
建议审计字段：

发起用户
请求时间
本地工具名
目标站点
风险等级
审批 ID
幂等键摘要
外部操作 ID
HTTP/MCP 结果状态
耗时
回读核验状态
错误类别
脱敏参数摘要
禁止记录：

完整 Token
Authorization Header
Cookie
密码
私钥
未脱敏隐私数据
整段敏感模型输入输出
不应长期保存的业务原始数据
9. OpenTelemetry 与 Langfuse
OpenTelemetry 追踪链路：

API Request
→ LangGraph Run
→ Graph Node
→ Tool Gateway
→ HTTP/MCP Request
→ External Verification
→ Final Response
建议 Trace 属性：

agent.tool.name
agent.tool.risk_level
agent.approval_id
agent.execution_status
agent.station_id（按敏感性决定是否脱敏）
http.status_code
external.operation_id（可脱敏）
禁止将完整工具参数、凭据和敏感正文写入 Span。

Langfuse 可用于：

模型调用
Agent Run
图节点
工具调用
Token 与成本
审批中断与恢复
评测结果
用户反馈
敏感业务场景建议：

使用自建 Langfuse；
禁止采集原始输入输出；
仅上传脱敏摘要、状态和性能指标。
第 10 批验收标准
HTTP 请求统一经 Adapter 发送。
模型和用户无法获得 URL、Token、Cookie、认证 Header。
所有工具调用经过注册、Schema、RBAC、站点授权、限流和审批检查。
无站点授权的用户不能查询、预览或写入。
只读授权不能执行写入。
MCP 远端新增工具不会自动暴露。
只读请求仅有限重试。
写操作网络异常不自动重发。
写操作使用服务端幂等键，并能按幂等键回读。
无法确认的外部执行保留为未知/待恢复状态。
Trace 能关联 API、LangGraph、工具、外部请求和核验。
审计与 Trace 不包含明文凭据和未脱敏敏感参数。
第 11 批：知识库/RAG、数据分级、权限过滤与离线评测
目标
建设可追溯、可授权、可重建和可评估的知识库能力，避免将文件直接丢给模型后无法判断来源、权限和可信度。

1. 文档入库管线
原始文件
  ↓
文件类型识别
  ↓
安全检查与敏感文件排除
  ↓
文本提取
  ↓
元数据提取
  ↓
切片
  ↓
向量化
  ↓
写入向量库 + 文档元数据表
  ↓
检索测试与引用验证
优先支持：

Markdown
TXT
PDF
Word
网页剪藏
Obsidian Vault
项目文档
会议纪要
默认排除：

.env
.env.*
*.pem
*.key
id_rsa
id_ed25519
credentials.json
token.json
cookies.txt
node_modules/
.venv/
.git/
__pycache__/
数据库导出
日志
构建产物
二进制文件
2. 文档与切片元数据
每份文档建议保存：

{
  "document_id": "project-a-architecture",
  "title": "项目 A 架构设计",
  "source_path": "data/knowledge/projects/project-a/architecture.md",
  "document_type": "project_document",
  "tags": ["project-a", "architecture"],
  "updated_at": "2026-11-02T00:00:00+08:00",
  "access_scope": "personal",
  "content_hash": "sha256:..."
}
每个切片建议保存：

{
  "chunk_id": "project-a-architecture-0012",
  "document_id": "project-a-architecture",
  "chunk_index": 12,
  "text": "……",
  "metadata": {
    "section": "工具架构",
    "page": 4
  }
}
3. 数据分级
建议按数据等级处理：

等级	示例	处理原则
公开	公开手册、公开规范	可正常检索
内部	项目文档、内部流程	限制授权用户
机密	客户资料、运维记录	强制权限过滤、最小化引用
高敏感	密钥、身份证件、Token、密码	默认禁止入库
4. 检索权限过滤
检索必须先过滤权限，再做向量相似度排序：

用户身份
→ 数据分级/访问范围过滤
→ 文档权限过滤
→ 向量检索
→ 重排序
→ 返回允许展示的片段与引用
禁止：

先检索全部资料
→ 再依赖模型“不要引用未授权内容”
因为未授权内容可能已经进入模型上下文。

5. RAG 回答规则
回答应明确区分：

资料明确说明：
根据资料推断：
当前资料不足以确认：
引用最少包含：

文档标题
章节或页码
更新时间
必要时来源路径或业务标识
当检索不足时，应直接说明：

当前知识库中未找到足以支持该结论的资料。

不得用模型常识填补为“资料结论”。

6. 离线评测与回归集
评测集建议覆盖：

知识问答正确性
引用准确性
权限过滤
找不到资料时拒绝编造
工具参数校验
只读/写入路由
审批流程
写操作超时状态
外部工具失败
敏感信息泄露
多轮会话恢复
站点越权访问
MCP 未注册工具调用
目录建议：

tests/
├── unit/
├── integration/
├── graph/
└── evals/
    ├── knowledge_qa.yaml
    ├── citation_accuracy.yaml
    ├── authorization.yaml
    ├── tool_calls.yaml
    ├── write_approval.yaml
    ├── safety.yaml
    └── regression.yaml
第 11 批验收标准
文件入库有来源、哈希、更新时间和数据分级。
敏感文件默认禁止入库。
检索前执行文档级权限过滤。
回答可给出可追溯引用。
检索不足时不会把模型推断伪装成资料原文。
可以按原始资料重建索引。
离线评测覆盖知识、权限、工具、审批和安全回归。
每次关键改动后可运行固定回归集，避免旧能力退化。
推荐实施顺序
第 8 批：审批、幂等、恢复
  ↓
第 9 批：LangGraph 状态机与中断恢复
  ↓
第 10 批：真实工具、RBAC、站点授权、限流、观测
  ↓
第 11 批：RAG、文档权限、引用与评测
原因：

先建立可靠写入边界，避免真实外部操作失控。
再将审批和恢复纳入 LangGraph 执行图。
然后接入真实 HTTP/MCP 工具及身份授权。
最后扩展知识库，确保资料检索也遵循权限、追溯和评测规范。
上线前最小检查清单
安全
.env、私钥、Token、Cookie 未提交 Git。
生产环境不允许直接信任客户端身份 Header。
生产环境启用 HTTPS、OIDC/身份网关和严格 CORS。
外部写操作具有单次审批、幂等键和回读机制。
审计、Trace 和模型上下文已完成必要脱敏。
未注册 HTTP/MCP 工具无法被模型调用。
高敏感文件不会进入知识库。
可靠性
数据库迁移使用 Alembic 管理。
服务重启后可恢复待确认审批。
写操作不因网络异常自动重发。
外部操作无法确认时保留未知状态。
同站点高风险操作具有并发保护。
HTTP/MCP 调用具备超时、分类错误和有限只读重试。
可观测性
API、LangGraph、工具和外部请求可通过 Trace 关联。
记录工具耗时、状态、审批 ID 和核验结果。
失败可区分认证、授权、参数、限流、网络、业务和核验失败。
关键流程有离线回归测试。
业务验收
未授权用户不能查询其他站点。
只读用户不能写入。
未审批不能执行控制。
已审批但超时的请求不能执行。
工具失败不会被描述为成功。
知识库回答可以提供正确引用。
资料不足时会明确说明无法确认。