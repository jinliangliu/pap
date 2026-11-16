# 多租户

一句话方案：**以可信身份令牌生成租户上下文，以数据库 RLS、检索过滤、对象存储 ACL、
工具网关鉴权、沙箱隔离和租户级凭据落实隔离；模型只处理已经授权的数据，不能作为权限判断者。**

本文是实现与运维口径。设计取舍与三阶段路线见
`docs/Personal Agent Platform：multi-tenant.md`，本文只记录**已经落地的东西**。

## 1. 隔离模型

采用默认组合：**共享 PostgreSQL + 共享表 + `tenant_id` + Row Level Security**。
高敏客户需要独立 schema / 独立库时，只换部署形态，不动应用代码。

三道防线，缺一不可：

| 层 | 做什么 | 兜住什么问题 |
|---|---|---|
| 认证层 | 从 Token 声明或可信网关确定 `tenant_id`，写入 ContextVar | 租户归属被客户端篡改 |
| 应用层 | ORM 事件自动追加过滤、写入校验 | 开发者忘记写 `where tenant_id = ...` |
| 数据库层 | PostgreSQL 事务级 RLS | 平台内其他服务/脚本直连数据库时的越权读 |

任何一层单独存在都不够：只做应用层过滤，一次 `session.execute(text(...))` 就绕过去了；
只做 RLS，SQLite/测试环境与连接池复用场景下行为差异太大。

## 2. 租户从哪来

| `AUTH_MODE` | 租户来源 | 说明 |
|---|---|---|
| `dev` | 请求头 `X-Authenticated-Tenant`，缺省 `DEFAULT_TENANT_ID` | 仅本地开发；`APP_ENV=production` 直接拒绝启动 |
| `gateway` | 可信身份网关（反代）注入的已认证声明 | 生产推荐：验签、JWKS、会话都由网关负责 |
| `jwt` | Bearer Token 的 `tenant_id` 声明 | 本服务自验签，**只接受 HS256** |

请求头里的 `X-Authenticated-Tenant` 在任何模式下都只是**切换申请**：

- 该用户必须是目标租户成员（`user_roles` 里有记录），否则 **403** 而不是 401；
- 403 与 401 必须分开：401 是「不知道你是谁」，403 是「知道你是谁，但你没这个租户的授权」；
- 请求体里的 `tenant_id` / `user_id` / 权限字段**一律忽略**，schema 里根本不声明它们。

租户成员关系就是 `user_roles(tenant_id, user_id, role_id)`——角色按租户解析，
同一个人在企业 A 是 `viewer`、在企业 B 是 `admin`，权限不跨租户复用。

## 3. 数据模型

两类表，边界必须清楚：

| 类别 | 表 | 是否自动过滤 |
|---|---|---|
| 身份平面 | `users` `roles` `role_permissions` `tenants` `tenant_quotas` | 否。账号与角色是跨租户的全局对象，成员关系在 `user_roles` |
| 业务平面 | `conversations` `messages` `agent_runs` `approval_requests` `audit_logs` `documents` `document_chunks` `memories` `user_roles` `station_grants` `artifacts` `tenant_tool_policies` | 是 |

业务表统一继承 `app/models/mixins.py::TenantScoped`：

```python
class Conversation(TenantScoped, Base):
    __tablename__ = "conversations"
    # tenant_id 由 mixin 提供：String(128), NOT NULL, index
```

继承即生效——新增业务表时不需要再登记到任何列表里（`_tenant_options()` 每次按
mapper 数量增量重建）。

## 4. 应用层：ORM 强制隔离

`app/tenancy/scope.py` 注册两个事件。

### 4.1 读：`do_orm_execute`

给每条 SELECT 追加 `with_loader_criteria`，覆盖当前语句里出现的所有租户表，
包括关系加载（lazy / selectin / joined）。

两个实现细节值得记下来，都是踩过的坑：

**（1）必须用绑定参数，不能用闭包捕获租户。**

SQLAlchemy 会缓存语句结构，闭包值不进缓存键。第一版写成
`lambda cls: cls.tenant_id == tenant`（并关掉 `track_closure_variables`），
结果是第二个租户复用了第一个租户的条件——测试里表现为「换租户查不到自己的数据」。

现在条件里是 `bindparam("pap_tenant_scope")`，值通过
`state.statement.params(...)` 逐次传入。语句结构可以安全复用，参数随执行走。

**（2）`bindparam` 的名字必须写字面量。**

SQLAlchemy 分析 lambda 时会把它的全局/闭包变量替换成 `PyWrapper` 代理用来追踪引用。
写 `bindparam(_TENANT_BIND)` 拿到的是 `PyWrapper` 而不是字符串，编译期炸在
`compiler.bindparam_string`：`TypeError: expected string or bytes-like object, got 'PyWrapper'`。
所以 `_tenant_criteria()` 里直接写字符串，并留了注释说明原因。

### 4.2 写：`before_flush`

| 情况 | 行为 |
|---|---|
| 新增，无 `tenant_id`，有上下文 | 自动填当前租户 |
| 新增，`tenant_id` 是别的租户 | 抛 `TenantIsolationError` |
| 新增，无 `tenant_id` 也无上下文 | 抛 `TenantContextMissingError`（除非 `TENANT_STRICT_WRITES=false`） |
| 修改，`tenant_id` 被重写 | 抛 `TenantIsolationError`——这是最直接的越权手法 |
| 修改/删除，行属于别的租户 | 抛 `TenantIsolationError` |

### 4.3 跨租户只有两条显式路径

```python
with system_tenant_scope():          # 维护任务：不允许从具体租户上下文进入
    ...

with tenant_scope(settings.platform_tenant_id):
    with platform_admin_tenant_scope(target):   # 平台管理：必须显式写出目标租户
        ...
```

两者都拒绝「顺手跨一下」：`system_tenant_scope()` 从具体租户上下文进入会直接报错，
`platform_admin_tenant_scope()` 要求当前上下文是平台租户。这样在代码里
`grep platform_admin_tenant_scope` 就能穷举所有跨租户操作。

单条查询需要看全量时用 `INCLUDE_ALL_TENANTS` 执行选项（`execution_options`），
不改变全局行为，便于审查。

## 5. 数据库层：Row Level Security

迁移 `alembic/versions/d3f1a9c2b7e4_multi_tenant.py` 对 13 张业务表执行：

```sql
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE  ROW LEVEL SECURITY;   -- 表 owner 也要受约束
CREATE POLICY tenant_isolation_conversations ON conversations
    USING (tenant_id = current_setting('app.tenant_id', true));
```

`app/tenancy/rls.py` 在引擎的 `begin` 事件里设置事务级变量：

```python
SELECT set_config('app.tenant_id', %s, true)   -- true = 事务级，随事务结束自动失效
```

覆盖的 13 张表：`conversations` `messages` `agent_runs` `approval_requests`
`audit_logs` `documents` `document_chunks` `memories` `station_grants`
`knowledge_index_state` `tenant_quotas` `tenant_tool_policies` `artifacts`。

**`user_roles` 故意不在此列**：成员关系在认证阶段就要读（此时还没有租户上下文），
且「这个人属于哪些租户」本身就是跨租户查询。它由 ORM 层过滤，访问路径只有两条：
按 `user_id` 查（认证）、按 `(tenant_id, user_id)` 查（授权）。

三个约束必须同时满足，否则 RLS 形同虚设：

1. **事务级**（`set_config(..., true)`）：会话级会随连接被下一个请求复用；
2. **`FORCE`**：不加则表 owner 与超级用户绕过策略，而迁移通常就是 owner 跑的；
3. **每条连接、每个事务都设**：靠 `begin` 事件保证，不依赖业务代码记得调用。

`SYSTEM_TENANT` 上下文下不设置变量——`current_setting(..., true)` 返回 NULL，
策略不成立，因此**跨租户任务必须在应用层显式逐租户遍历**。维护任务就是这么改的：

```python
for tenant_id in await _active_tenants():
    with tenant_scope(tenant_id):
        await _sweep()
```

SQLite 上没有 RLS，`install_tenant_rls_hook()` 直接返回，隔离由 ORM 层承担
（这也意味着 SQLite 只适合本地开发与测试）。

### 5.1 生产部署：应用角色与 DDL 边界

RLS 能不能挡住人，取决于**应用拿什么角色连库**。用超级用户（或带 `BYPASSRLS`）
连接时，策略完全不生效——`FORCE ROW LEVEL SECURITY` 也只约束表 owner，约束不了
超级用户。这种情况下仍然只有 ORM 一层在工作，而且**没有任何报错**，很容易误判。

所以生产必须把「跑应用的账号」与「改结构的账号」分开：

```sql
-- 应用账号：能读写数据，不能改结构
CREATE ROLE pap_app LOGIN PASSWORD '<strong-password>'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

GRANT CONNECT ON DATABASE personal_agent TO pap_app;
GRANT USAGE  ON SCHEMA public TO pap_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO pap_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO pap_app;

-- 迁移用管理账号建的新表，自动继承同样的权限
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pap_app;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO pap_app;

-- 结构版本由管理账号维护，应用不需要也不应该读它
REVOKE ALL ON alembic_version FROM pap_app;
```

**没有 `CREATE ON SCHEMA`** 是有意的：应用运行时能建表，就等于能建一张不受
RLS 保护的表来放数据。代价是 LangGraph checkpointer 的 `setup()` 不能在启动时跑
（它执行 `CREATE TABLE IF NOT EXISTS`，同样需要 schema 的 CREATE 权限），
于是有了 `CHECKPOINT_AUTO_SETUP=false` 与离线初始化脚本。

部署顺序不能颠倒：

```bash
# 1. 业务表（管理账号连接串覆盖 DATABASE_URL，只作用于这一条命令）
DATABASE_URL='postgresql+psycopg://<owner>:...@127.0.0.1:5432/personal_agent' \
    uv run alembic upgrade head

# 2. checkpoint 表（幂等，可重复执行）
uv run python -m scripts.init_checkpointer --url 'postgresql://<owner>:...@127.0.0.1:5432/personal_agent'

# 3. 重启应用
systemctl restart personal-agent-platform
```

`.env` 里 `DATABASE_URL` / `CHECKPOINT_DATABASE_URL` 用 `pap_app`，
`CHECKPOINT_AUTO_SETUP=false`。

更换账号后必须实测，不要只看服务起来了：

```sql
-- 不设上下文：必须 0 行（默认拒绝，而不是全表可见）
SELECT count(*) FROM conversations;

-- 设成某个租户：只能看到该租户的行
BEGIN; SELECT set_config('app.tenant_id', 'default', true);
        SELECT count(*) FROM conversations; COMMIT;
```

两条结果分别为 `0` 和有数据，才说明 RLS 真的生效。若第一条返回全表行数，
说明连接账号仍然有绕过权限。

## 6. 会话线程命名空间

LangGraph 的 checkpointer 用 `thread_id` 定位会话状态。若直接使用客户端传来的
`thread_id`，猜到别人的 ID 就能恢复他人中断现场（包括待审批的工具参数）。

现在内部 ID 由服务端拼成：

```
{tenant_id}:{user_id}:{client_thread_id}
```

- 客户端只看到 `client_thread_id`，响应里原样返回；
- `client_thread_id` 有字符集与长度限制（`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`）；
- 即使客户端把别人的**内部** ID 当 `client_thread_id` 传进来，也只会得到自己命名空间下的新会话。

已知边界：checkpointer 自己的表（`checkpoints` / `checkpoint_blobs` /
`checkpoint_writes`）由 LangGraph 管理，没有 `tenant_id` 列，隔离完全依赖这个命名空间。
所以线程 ID 的拼装规则属于安全边界，不能改成「客户端 ID 直接用」。

## 7. 子资源隔离

| 资源 | 手段 | 越权结果 |
|---|---|---|
| 会话 / 消息 / 运行 | ORM 自动过滤 + 线程命名空间 | 查不到，`404` |
| 审批单 | 查询显式带 `tenant_id` + `user_id` | `404` |
| 知识切片 | 检索前 ACL 过滤（见下） | 不进入候选，模型看不到 |
| 附件 | 元数据按 `(tenant, user)` 过滤 + 对象键前缀二次校验 | `404` |
| 沙箱执行记录 | `X-Sandbox-Tenant` / `X-Sandbox-User` 归属，同租户内再按用户比对 | `404` |
| 工具调用 | 租户策略 + 角色 + 站点授权 | 拒绝执行 |

统一用 `404` 而不是 `403`：不确认「这个 ID 存在但你没权限」。

### 7.1 知识切片 ACL

切片带四元组：`access_scope`（public/internal/confidential）、`visibility`
（`tenant_shared` / `user_private`）、`allowed_roles`、`owner_user_id`。

**顺序是硬要求：先过滤，后打分。** 未授权切片不进入 BM25 候选，
既不参与排序，也不可能出现在 citations 或模型上下文里。

```
候选切片 → access_scope 过滤 → 可见性/角色/属主过滤 → BM25 打分 → top_k → 引用
```

不要改成「先检索再让模型忽略」：模型不是权限判断者，把内容放进上下文就已有泄露。

`user_private` 的切片在入库阶段就必须带 `owner_user_id`，缺了直接拒绝入库
（`app/knowledge/ingest.py`），避免产生「谁都看不见 / 谁都能看见」的脏数据。

### 7.2 附件

- 对象键由服务端生成：`tenants/<tenant>/users/<user>/artifacts/<uuid><suffix>`；
- 键里带租户**不是**权限——下载每次都按 `(tenant_id, user_id)` 查元数据，
  再校验键前缀与归属一致（双保险）；
- 一律 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`，
  上传的 HTML/SVG 不会被同源执行；`text/html`、`image/svg+xml` 等强制降级为
  `application/octet-stream`；
- 对象只存 `object_key`，不存密钥、不存可直链 URL。

## 8. 工具网关

判定顺序：注册 → 启用 → JSON Schema → 角色/Scope → **租户策略** → 站点授权 → 限流 → 审批。

租户策略（`tenant_tool_policies`）三件事：

| 字段 | 作用 |
|---|---|
| `enabled` | 该租户是否允许用这个工具 |
| `allowed_roles` | 租户内进一步收窄到某些角色（空 = 租户内不限） |
| `credential_ref` | 调外部系统用的凭据**引用**（环境变量名） |

凭据规则：

- 库里只存引用，形如 `STATION_TOKEN_ACME`，正则 `^[A-Z][A-Z0-9_]{0,127}$`；
- 运行时从环境变量解析，只放进**本次调用**的 `ToolExecutionContext.credential`；
- 不做插值、不读文件、不落日志；解析不到就按「无凭据」继续，由下游决定是否拒绝；
- 静默复用别的租户凭据是灾难级问题，所以这里宁可失败在明处。

缓存：策略与配额在进程内缓存 `TENANT_POLICY_CACHE_SECONDS`（默认 5s）。
管理接口写入后会主动失效**本进程**缓存；多实例部署时其他实例最多晚一个周期。

## 9. 配额与限流

键里必须有租户，否则一个租户刷量会影响另一个：

```
rate-limit:t:{tenant}:u:{user}:{tool}:{bucket}      # 用户 × 工具 × 分钟
rate-limit:t:{tenant}:tool:{bucket}                 # 租户总工具调用 × 分钟
quota:t:{tenant}:chat:{yyyymmdd}                    # 租户日对话数
```

- 日配额在落库**之前**扣：超限返回 `status="rejected"`，不留半条会话记录；
- 后端必须是 Redis（跨进程计数），生产环境配 `memory` 会拒绝启动；
- Redis 不可用时默认 fail-closed；`RATE_LIMIT_FAIL_OPEN=true` 才放行（并打日志）。

限流不能代替审批、幂等与外部回读——它只解决资源争抢。

## 10. 平台管理通道

`/api/v1/admin/*` 是唯一能跨租户的 HTTP 路径，进入条件是
「平台租户成员 + `platform_admin` 角色」，并且每个操作都在
`platform_admin_tenant_scope(target)` 里执行、写一条 `ADMIN_ACTION` 审计
（谁、对哪个租户、做了什么）。

能力边界是刻意的：只管理**租户、成员、配额、工具策略、审计**。
不提供读取租户会话内容、知识库原文、附件内容的接口——运维便利不能变成数据后门。
需要排查时走审计与指标，而不是「给我看看这个租户的对话」。

## 11. 运维

### 11.1 开户

```bash
# 建租户 + 加成员 + 签发调试 Token
uv run python -m scripts.create_tenant --tenant acme --name "ACME" \
    --member alice --role ops --daily-chat 2000 --token alice
```

也可以走 API（平台管理员身份）：

```bash
curl -X POST https://host/api/v1/admin/tenants \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"tenant_id":"acme","name":"ACME","daily_chat_requests":2000}'

curl -X PUT https://host/api/v1/admin/tenants/acme/members \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"subject":"alice","role":"ops"}'
```

### 11.2 开发数据

```bash
uv run python -m scripts.seed_dev_data --tenant default --subject dev-user
uv run python -m scripts.ingest_knowledge --tenant default
```

### 11.3 迁移与验证

```bash
uv run alembic upgrade head
uv run alembic check                     # 模型与迁移不得漂移
```

RLS 是否真的生效，用**非 owner 角色**连库验证（superuser 与表 owner 默认绕过）：

```sql
-- 期望：0 行（未设置 app.tenant_id）
SELECT count(*) FROM conversations;

BEGIN;
SELECT set_config('app.tenant_id', 'acme', true);
SELECT count(*) FROM conversations;   -- 只有 acme 的行
COMMIT;
```

## 12. 已知边界

写在这里是为了避免「以为已经隔离了」：

| 边界 | 现状 | 应对 |
|---|---|---|
| Core 语句（`text()` / `update()` / `delete()`） | 不自动加条件 | 必须显式带 `tenant_id`；数据库层 RLS 兜底 |
| `user_roles` 无 RLS | 认证先于租户绑定 | ORM 层按 `tenant_id` 过滤；避免用裸 SQL 读该表 |
| SQLite 部署 | 无 RLS | 只用于本地开发与测试 |
| 应用连接若是超级用户 / `BYPASSRLS` | RLS 被跳过，且无任何告警 | 生产用普通角色连库（5.1 节），迁移用另一个账号 |
| LangGraph checkpointer | 无 `tenant_id` 列 | 依赖线程 ID 命名空间，规则不可放宽 |
| checkpointer 建表 | 应用没有 schema `CREATE` 权限 | `CHECKPOINT_AUTO_SETUP=false` + `scripts.init_checkpointer` 离线建 |
| 多实例策略缓存 | 最多晚一个缓存周期 | 敏感变更后等一个周期，或调用管理接口触发本进程失效 |
| 平台管理员 | 有意拥有跨租户运维能力 | 只有一条代码路径 + 全量审计 |
| 审计表本身 | 按租户写入，跨租户查看走管理通道 | 不提供「一次看全部租户审计」的普通接口 |

## 13. 验收清单

第一阶段「必须具备」逐项对应：

- [x] OIDC/JWT 身份认证 —— `AUTH_MODE=jwt`（HS256 自验签）/ `gateway`
- [x] 服务端可信注入 `tenant_id` / `user_id` —— 认证依赖 + ContextVar，请求体不参与
- [x] 所有业务数据表含 `tenant_id` —— `TenantScoped`
- [x] PostgreSQL RLS —— 13 张表 `ENABLE` + `FORCE` + policy
- [x] 会话 / 文件 / 知识库 / 工具调用按租户过滤 —— 见第 6、7、8 节
- [x] 每租户独立对象存储前缀 —— `tenants/<tenant>/users/<user>/artifacts/`
- [x] 工具凭据按租户保存 —— `credential_ref` 指向环境变量，密钥不落库
- [x] 全链路审计字段 —— `audit_logs.tenant_id` + `ADMIN_ACTION`
- [x] 基础限流与配额 —— 租户 × 用户 × 工具 + 租户日配额
- [x] 默认禁止跨租户管理员查询 —— 只有平台租户 `platform_admin` 一条显式路径

测试覆盖：`tests/unit/test_tenancy.py`（上下文与 ORM 强制）、
`tests/unit/test_tenant_policy.py`（策略与凭据）、
`tests/integration/test_multi_tenant.py`（跨租户越权、配额、管理通道审计）、
`tests/integration/test_knowledge_store.py`（切片 ACL）。
