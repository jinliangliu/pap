# 连接器（Connectors）

**一句话**：连接器是「一个外部系统」的接入点。端点和认证方式只写一次，凭据按租户各存一份，
它下面挂的工具共享这套配置。

## 1. 为什么需要这一层

在连接器之前，接一个外部系统是「一条工具一条配置」：

- HTTP 系统十个接口 = `custom_tools` 十条记录，`url` 里把端点抄十遍；
- MCP 系统十个工具 = `mcp_registered_tools` 十行，`server_url` / `token` 各抄十遍；
- 端点换了，要改十处，漏一处就是「一个工具能用、另一个连不上」。

两个更麻烦的问题：

1. **凭据是平台级的**。`mcp_registered_tools.token` 一行一个令牌，全租户共用；
   `custom_tools` 干脆没有凭据字段，只能靠 `tenant_tool_policies.credential_ref`
   指向环境变量。前者意味着「出事定位不到人」，后者意味着「换个 token 要改部署」。
2. **边界只能列工具名**。给一个智能体开一个系统的能力，要在
   `allowed_tools` 里写二十个名字；`revision` 跟着 bump，在途审批单的快照跟着作废。

连接器一次解决这三件事。它在 `docs/MCP_INTEGRATION.md` §2.1 里被列为
「没有做的事」之一（按租户区分外部端点凭据），本文是把它补上之后的说明。

---

## 2. 两层结构：定义是平台级，凭据是租户级

| 表 | 作用域 | 关键字段 |
|---|---|---|
| `connectors` | **平台级**（不继承 `TenantScoped`，工具名全局唯一） | `name`（唯一标识）、`kind`(`http`/`mcp`)、`base_url`、`auth_type`、`auth_header_name`、`timeout_seconds`、`enabled`、`health_status` |
| `connector_credentials` | **租户级**（继承 `TenantScoped`，挂 RLS） | `connector_id`、`username`、`secret`，唯一键 `(tenant_id, connector_id)` |
| `custom_tools` / `mcp_registered_tools` | 各自原有作用域 | 新增 `connector_id`：非空表示这套端点和凭据来自连接器 |

为什么这么切：

- **定义必须唯一**。两个租户各建一份「飞书」连接器，工具名会撞、审计会分叉，
  而端点本来就该由平台统一（谁能把 `base_url` 指向自己的服务，谁就能把全平台的数据
  导过去）。
- **凭据必须分开**。外部账号天然属于某个组织。平台级令牌等于让所有人共用一个账号：
  出问题时审计里只有「平台用过这个令牌」，没有「谁用的」。

`auth_type` 刻意**不提供 `oauth2`**：授权码流程要浏览器回调、令牌刷新、按用户绑定，
和「一条服务端配置」不是同一类东西。硬塞进来会得到一个「看起来支持 OAuth、
实际要人工每月换一次 token」的假能力。

MCP 连接器只允许 `none` / `bearer`：Streamable HTTP 只认 `Authorization: Bearer`。
允许配 header/basic 的结果是「凭据明明填了还是 401」——所有配置看起来都对，
只有抓包能看出来。

---

## 3. 三条不变量

### 3.1 凭据不进闭包，只经 `ToolExecutionContext` 进单次调用

工具 handler 是**进程级、跨租户**的：同一个函数对象服务所有租户。凭据一旦进闭包
（或模块变量、或工具定义），第一个租户写进去的令牌就会被第二个租户用掉，
而返回值一模一样，谁也不会发现。

```python
@dataclass(frozen=True, slots=True)
class ConnectorRuntime:
    id: int
    name: str
    ...
    # repr=False：这个对象会进异常 traceback 与调试日志，
    # frozen dataclass 的默认 repr 会把令牌原样打出来。
    secret: str | None = field(default=None, repr=False)
```

网关在 `_with_tenant_credential` 里按 `tool.connector_id` 解析出 runtime，
放进 `context.connector`；handler 只从 `context.connector.auth_headers()` 取头。

### 3.2 凭据缺失 fail-closed，且绝不回退

没配凭据时直接拒绝（`CONNECTOR_CREDENTIAL_MISSING`，文案写明「去控制台填凭据」），
**不回退到 `.env`、也不回退到工具行上的 `token`**。

原因是不对称：回退能让「忘了配」表现成「用别人的账号调通了」，而返回值完全正常；
不回退最差只是「多配一次」。前者是安全问题，后者是体验问题。

### 3.3 连接器完全接管，不与行上字段混用

工具挂了连接器，`mcp_registered_tools.server_url` / `token` 就**不参与**调用；
`custom_tools.url` 若是相对路径则拼到 `connector.base_url` 后面，绝对 URL 原样使用
（已经写全的配置不该因为归到某个名字下就失效）。

不做「优先级」是因为：两处都能指定端点时，「后改的那处不生效」是最难查的一类错。
与其定义谁优先，不如让连接器全权接管——要改端点，就改连接器。

---

## 4. 凭据缺失 ≠ 从目录隐藏

这是本模块最容易做错的一处判断。

| 情况 | 目录里 | 调用时 |
|---|---|---|
| 权限不足（不在 `allowed_tools` / `allowed_connectors` 里） | **隐藏** | 不适用 |
| 智能体边界外 | **隐藏** | 不适用 |
| 连接器已停用 | **隐藏** | 不适用 |
| 凭据没配 | **保留** | 拒绝，并给可行动的下一步 |

前三种是「不该看见」，第四种只是「还没配好」——运维状态，换令牌期间会短暂缺失。
把它做成「从目录里消失」，模型会以为自己没这个能力，然后换一条路去猜；
而人看到的是「我明明配了，它就是不用」。

---

## 5. 边界：工具与连接器是并集

`agents.allowed_tools` 与 `agents.allowed_connectors` 是**并集**关系：

- 工具名命中 `allowed_tools`，或它所属的连接器在 `allowed_connectors` 里 → 可见；
- 两者都是 `None` → 不额外收窄（等同旧行为）；
- **只配连接器**时，内置工具会消失。这是刻意的：人明确表达了「只给这些」。

代价是连接器内新增的工具会自动进入边界——这是有意的设计，连接器是**能力组**，
不是「当时那几个工具的清单」。反过来（新增工具默认不可见）会让「接一个新接口」
变成两步容易忘的操作。

子集判定（委派）也跟着扩展：`sub.allowed_connectors` 不在 `sup.allowed_connectors`
里 → 拒绝；`sup.allowed_tools` 已收窄但 `sup.allowed_connectors is None` 时也保守拒绝
——纯函数看不到连接器下有二十个工具，放行的代价是「A 只给了三个工具，
B 凭一个名字拿到二十个」。

---

## 6. 停用与删除

**停用连接器 = 它的工具一起从目录消失。** 判定用 `ToolDefinition.connector_ready`
（注册时从连接器快照来的），因为目录生成是同步路径，拿不到库。

**删除保护**：下面还挂着工具时拒绝删除（400），**不**自动把工具变成「独立工具」。
后者会把凭据来源从连接器切到 `.env`，可能从「能用」变成「用别人的令牌」。

**凭据清理**分两步，这是被 RLS 与租户隔离逼出来的：

1. 删除接口当场清**当前租户**的凭据（`purge_credentials`）；
2. 其余租户的行由维护任务逐租户扫（`purge_orphan_credentials`，
   在 `app/workers/maintenance.py` 的每租户循环里）。

为什么不能一次清完：`connector_credentials` 有行级安全，跨租户清理要逐个租户开
`tenant_scope`，而请求路径上禁止跨租户切换（硬切抛 `TenantIsolationError`，
表现成「连接器已删、接口 500」）。这期间孤儿凭据有害吗？没有：读取凭据必须先从
`connectors` 取到行，连接器已删，任何路径都拿不到它。

---

## 7. 健康检查与「发现」

- 健康检查用**调用者租户的凭据**跑。拿别人的凭据测出「正常」，只会让人对着 401
  反复怀疑自己。
- 「发现」与「接入」必须用同一个端点、同一份凭据，否则报出来的错会指向另一个服务
  （`MCP_INTEGRATION.md` §2.1 踩过这个坑）。
- `health_status` 是 `String` 不是原生枚举：它是运维派生量，以后还会加值
  （`degraded`），PG 原生枚举加值要 `ALTER TYPE` 且不能在事务块里回滚。
- `unknown` 是初始态，**不等于健康**。界面上「未检查」与「连通」必须分开，
  否则「从没配过凭据」和「刚刚测通」看起来一模一样。

HTTP 连接器的判定：

| 响应 | 结论 |
|---|---|
| 401 / 403 | 凭据问题（「凭据被拒绝」） |
| 5xx | 对端故障 |
| 其它 4xx | 端点可达（只是这个路径不存在），算连通 |

---

## 8. 批量接入，默认风险「写入」

一次接二十个工具时，没人会逐个判断风险等级。所以批量接入的默认值是
**写入**（`BIND_DEFAULT_RISK`），不是读取：

- 默认「读取」→ 远端一个 `delete_*` 接进来就是**免审批直接执行**，而它的名字
  和别的工具长得一样；
- 默认「写入」→ 代价只是多点一次确认。

这两者不对等，所以取严的一侧。界面上这个默认值在「发现工具」面板里显式可选，
接入后也能在工具页逐个改。

非法等级（`risk_level="banana"`）返回 400 而不是 500：那是请求内容的问题。

---

## 9. 接口一览

两级权限，不是一个：

- **定义**（建 / 改 / 删 / 发现 / 绑定 / 健康检查）：平台管理员。改一次影响所有租户。
- **凭据**：`connector:write` 或平台管理员，写在自己所属的租户下。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/connectors` | 列表，附**本租户**的 `credential_set` 与 `tool_count` |
| GET | `/api/v1/connectors/{name}` | 单条 |
| POST | `/api/v1/connectors` | 新建（名称须为 `[a-z][a-z0-9_]{1,63}`） |
| PATCH | `/api/v1/connectors/{name}` | 改定义，改完立刻重注册下属工具 |
| DELETE | `/api/v1/connectors/{name}` | 挂着工具时 400 |
| PUT / DELETE | `/api/v1/connectors/{name}/credential` | 写 / 清本租户凭据，**永不回显** |
| POST | `/api/v1/connectors/{name}/check` | 健康检查 |
| POST | `/api/v1/connectors/{name}/discover` | 列出远端工具，标出已接入的 |
| POST | `/api/v1/connectors/{name}/tools` | 批量接入 `{remote_name, local_name, risk_level?}` |
| DELETE | `/api/v1/connectors/{name}/tools/{local_name}` | 取消接入 |
| GET | `/api/v1/connectors/{name}/tools` | 已接入的工具 |

审计事件：`connector_created` / `connector_updated` / `connector_deleted` /
`connector_credential_set` / `connector_credential_cleared` /
`connector_health_checked` / `connector_tools_bound` / `connector_tool_unbound`。
凭据**只记动作不记值**——审计表长期保留，一个令牌写进去就等于永久泄露。

---

## 10. 运维要点

- **迁移**：`b7c4d2e9f831_connectors.py`。存量行 `connector_id` 全为 NULL，
  行为不变。**不做数据回填**：把二十条散工具自动猜成三个连接器，猜错比不做更糟。
- **启动加载**：`load_connector_tools` 在 `load_custom_tools` 之后跑，
  按连接器分组拉一次远端清单。某个连接器连不上只影响它自己的工具，启动不失败。
- **缓存**：运行时解析结果缓存 30 秒，设置/清除凭据、改定义、删连接器后立刻失效。
  否则会出现「刚配好凭据，助手还说没配」。
- **`MCP_ENABLED` 不管连接器**。那个开关管的是旧路径（全局单一端点 + 代码白名单 +
  环境变量令牌）；连接器自带 `enabled`，且建/改/启停都要平台管理员并落审计。
  绑在一起会出现「连接器页显示已接入、工具目录里却没有」这种只在调用失败时
  才暴露的状态。
- **Studio 助手**能建连接器（`studio_connector_save` / `studio_connector_delete`），
  但**不接受凭据**：这套工具在描述里就写明不得索取、不得让用户在对话里发送令牌。

---

## 11. 没有做的事

- **OAuth2 / 每用户授权**。见 §2 的理由。真需要时是另一个模型（授权流程 + 令牌刷新
  + 按人绑定），不是给 `auth_type` 加个值。
- **连接器级配额与限流**。现在只有平台级与工具级限流。一个连接器被一个租户打满，
  会影响同连接器上的其他租户。
- **端点连通性巡检**。健康检查是手动的（或按需触发），没有定时巡检；
  所以 `health_status` 可能很久以前测过，界面上的时间戳要看。
- **凭据加密存储**。与 `model_providers.api_key`、`mcp_registered_tools.token`
  同一条现状：库里明文，靠数据库与备份的访问控制。要做就三处一起做，
  只加密一处会让人以为整体都加密了。
- **连接器之间的故障转移**。一个工具只属于一个连接器。
