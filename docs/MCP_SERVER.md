# 对外 MCP 通道（入向）

PAP 作为 **MCP 服务端**，被外部宿主（WorkBuddy 连接器等）调用。
方向不要搞错：`app/tools/mcp_tools.py` 与 `app/services/connector_service.py`
是**出向**（PAP 当客户端去连别人的 MCP 服务）；本文讲的是**入向**。

## 1. 为什么只有一个能力入口

PAP 是多智能体平台，它本身就是智能体。所以对外暴露的不是「查台账」「查知识库」
「跑编排」这一组函数，而是**一次委派**：

| 工具 | 性质 |
|---|---|
| `pap_delegate(task, conversation_id?, wait_seconds?)` | 唯一的能力入口。一整段意图交过去，PAP 自己决定用哪个 Agent、调哪些数据源 |
| `pap_task_status(task_id)` | 不是能力，是句柄查询。超时之后靠它取回结果 |

拆成一组细粒度工具会带来两个具体后果：

1. **调用方被迫做规划。** 它得自己记住上一跳的结论、判断下一步该调哪个工具，
   而这些判断的上下文（站点、型号、告警等级）恰恰只有 PAP 的规划器能看到全集。
2. **分工改动不可见。** PAP 内部调整「谁负责什么」，连接器那边还钉在旧工具上，
   显示「已连接」而行为已经过时。

同理，`pap_delegate` **不接受 `agent` 参数**。要按角色分，就做成两个连接器
（平台入口 / 现场运维），而不是一个连接器加一个参数——连接器的粒度应该和
「用户会向谁交代事情」对齐。

## 2. 任务状态机

```
委派 ──► 进行中 ──┬─► completed          结论 + 引用 + 执行轨迹
                  │    └─ tool_error     └─ 办完了但没拿到数据，answer 是原因
                  ├─► needs_input        问题带回用户，再委派一次
                  ├─► awaiting_approval  确认留在 PAP 控制台
                  ├─► running            超时移交，用 task_id 续查
                  └─► failed             原因在 error
```

两种「交回给人」的状态不能折成 `completed`。对人没差别，对程序化调用方是致命的：
`completed` + 一句反问会被当成结论转述出去，而事实是这一轮什么都没办成。
所以 `ChatResponse.status` 新增了 `needs_input`——**状态是平台事实，
不该由每个客户端拿 `metadata.clarify` 非空去猜**。

`ChatResponse` 还有 `error`、`rejected` 两个内部取值，对外一律折成 `failed`：
工具契约只有五个状态，第六个值会掉进调用方的 `default` 分支，而那通常是
「当作正常结束」或者什么都不做。`_STATUS_ALIASES` 是唯一折叠点。

### 两个错误字段的分工

| 字段 | 含义 | 出现在 |
|---|---|---|
| `error` | **任务没办成**，调用方必须处理 | 只有 `status=failed` |
| `tool_error` | 任务办完了，但某个工具失败，结论里没有数据 | 其它状态，实际只会碰上 `completed` |

不能合成一个字段。实测踩过：`status=completed` 同时带 `error` 时，
调用方一边看到「完成」一边看到「错误」，最后「什么都没查到」被当成正常结论
转述出去了。所以带 `tool_error` 的 `completed` 会换一句 hint——
「PAP 没能拿到数据：answer 里是原因，不是结论」。

### 返回体

```json
{
  "task_id": "<run_id>",
  "status": "needs_input",
  "conversation_id": "mcp-1a2b3c4d",
  "answer": "还差一个站点编号。",
  "questions": [{"question": "涉及哪个站点？", "options": ["station-001"]}],
  "citations": [{"source": "TCGAS 027-2023", "snippet": "…"}],
  "trace": {"agent": "assistant", "intent": "clarify", "tool": null},
  "hint": "把 questions 拿去问用户，拿到答案后带上同一个 conversation_id 再委派一次。"
}
```

`hint` 是接口契约，不是提示词修辞。每个状态都带一句，写死三件事：
不要重写结论、不要拆解任务、不要原样重试。没有这层，PAP 查到的规程会被
上层模型洗成它自己「知道」的事，出了错连是哪一步错的都追不回来。

`conversation_id` 就是 PAP 的 `thread_id`（客户端可见部分）。同一件事分几次交代
带上它，PAP 会接着上文；省略表示开一件新的事。

### 能力声明（`allow_write` 与工具注解）

`pap_delegate` 有一个 `allow_write` 参数，默认 `true`。传 `false` 时这一轮退化为
只读：落库时 `agent_can_write=False`，网关会**把写风险工具从能力目录里摘掉**，
规划器看不到、也调不到，不会走到审批那一跳。返回体带 `"read_only": true`，
调用方据此解释「为什么没落单」——不是 PAP 不会写，是这次没给它写的能力。

**它只能收窄，不能放宽。** 能不能写是智能体自身的属性（`agents.can_write`），
调用方无权凭一次参数取得它。`_capability_boundary` 只有一个改写方向：
`True → False`；对已经只读的智能体传 `allow_write=true` 也不会变回可写。
写反了这个方向的后果不是报错，而是「MCP 变成一条绕过智能体权限配置的通道，
而表面上一切正常」——所以它单独有两条单测守着。

工具注解是另一层，**给宿主看的，不是给 PAP 执行的**：

| 工具 | readOnly | destructive | idempotent | openWorld |
|---|---|---|---|---|
| `pap_delegate` | `false` | `false` | `false` | `true` |
| `pap_task_status` | `true` | `false` | `true` | `false` |

`readOnlyHint=false` 是这里唯一重要的位：宿主看到它才会在自动执行前提醒用户。
`destructiveHint=false` 是实话——PAP 的写操作是落一张工单，不是删改现场数据。
注解**不构成服务端约束**，真正挡住写操作的是网关与图内审批。

## 3. 时限

`MCP_INBOUND_WAIT_SECONDS` 默认 30 秒，**远小于** `CHAT_TIMEOUT_SECONDS`(160)。
理由：MCP 客户端自己会先超时，让请求挂 160 秒的结果是调用方看到连接中断，
而不是「还在跑」。

超时之后这一轮**不会取消**，它继续在同一个事件循环里跑完并落库；
`pap_task_status` 按 `agent_runs` 读回状态与正文。强引用存在
`tools._RUNNING` 里——`asyncio` 只持弱引用，不留引用会表现为
「委派一开始就没了、日志里什么都没有」。

进程重启会丢掉在跑的那一轮（协程随进程消失），run 行由
`interrupt_stale_runs()` 标成中断。这是已知边界，不是缺陷。

## 4. 鉴权：个人访问令牌（PAT）

调用方带 `Authorization: Bearer <token>`。两级凭据，都收敛到同一个 `Identity`：

| 凭据 | 用途 | 主体来自 |
|---|---|---|
| **个人访问令牌**（`api_tokens` 表） | 正常路径。控制台「访问令牌」自助签发 | 令牌行 → `users.external_subject` → `resolve_identity` |
| **静态兜底令牌**（`MCP_INBOUND_TOKEN`） | 单机自测、表还没建好的时刻 | `MCP_INBOUND_SUBJECT` 配置 |

三条硬规则：

1. **主体只能来自凭据，绝不来自请求头。** 带令牌的调用方若能自选身份，
   这张令牌就是万能钥匙，租户隔离当场失效。
2. **令牌不带角色。** 角色、权限、站点授权每次现查——撤角色必须立刻生效，
   而不是等令牌过期。
3. **无凭据即全拒。** 两条路都没有可用凭据时，`BearerGate` 一律 401。
   这是 fail-closed，不是「没有凭据就放行」。因此启动时**不再要求**必须配
   静态令牌：那会逼着运维长期留一枚谁都在用的万能令牌。

### 令牌的形态

`pap_` + 32 字节随机（256 bit）。库里只存 `sha256`，明文只在签发响应里出现一次。
不加 pepper：这个长度没有可枚举空间，加 pepper 只会引入一个
「忘了配就悄悄变弱」的配置项。真正需要保护的是**传输**——明文 HTTP 上
谁都能读到令牌，哈希救不了这一点。

### 一次调用的完整路径

```
Authorization: Bearer pap_xxx
  → BearerGate（ASGI 门槛，挂载点外）
      → 静态令牌？命中则用配置里的主体
      → 否则按 sha256 查 api_tokens → 有效？→ resolve_identity
      → 失败一律 401（不区分不存在/过期/已撤销，避免给爆破者信号）
      → 成功把 Identity 放进 scope["state"]，交给下游
  → 工具从 ctx.request_context.request.state 取，不再查库
```

**为什么身份在门槛里解析而不是在工具里**：`initialize`/`tools/list` 这些
JSON-RPC 方法也要过同一道门。只在工具里校验的话，一枚无效令牌会先建立会话、
再收到一个工具级错误，客户端看到的是「工具调用失败」而不是「凭据不对」。

`last_used_at` 在鉴权热路径上，超过一分钟才写一次，失败只记日志——
记账不该让一次合法调用失败。

审计里所有调用都记在令牌所属的人身上（`api_token_issued` / `api_token_revoked`
两个事件留痕，令牌值永不进审计表）。

## 5. 实现要点

`app/mcp_server/`：

| 文件 | 职责 |
|---|---|
| `auth.py` | Bearer 校验（`BearerGate`，裸 ASGI）、令牌 → `Identity`（PAT + 静态兜底） |
| `tools.py` | `delegate()` / `task_status()` 两个实体，加上只负责取身份的 MCP 薄壳 |
| `server.py` | `MCPServer` 单例、`streamable_http_app()` 的构造、挂载用的 ASGI |
| `upload.py` | `/mcp/upload` 端点（见 §6）。分流放在 `BearerGate` **内侧**——放外面它就成了无鉴权的写入口 |

共享的入库实现在 `app/knowledge/upload.py`（`ingest_upload`），REST 与 MCP 两条传输
都调它一份，见 §6。

令牌的另一半在别处：`app/models/api_token.py`（表）、
`app/services/api_token_service.py`（签发/校验/撤销）、
`app/api/routes/me.py`（`/api/v1/me/tokens`）、控制台「访问令牌」页。

**`api_tokens` 刻意不挂 RLS**：鉴权发生在知道租户之前，查询当时没有
`app.tenant_id` 可填。挂上策略之后那条查询永远返回空，表现是
「令牌签出来了但怎么都用不了」。与 `users` / `roles` 同属身份平面，
读它的每一处都必须自己带 `user_id` 条件。

三条不显眼但会咬人的地方：

1. **会话管理器必须自己 run。** `streamable_http_app()` 返回的 Starlette 应用
   自带 lifespan，但被 FastAPI `mount` 之后它的 lifespan **不会执行**
   （Starlette 不往子应用传 lifespan）。只挂不 run 的表现是
   「端点存在、每个请求都 500」。所以只取它内部的 `session_manager`，
   在 `app/main.py` 的 lifespan 里 `async with run()`。
2. **挂在根路径，不挂 `/mcp`。** 挂 `/mcp` 时 Starlette 会用 **307**
   把 `/mcp` 重定向到 `/mcp/`（Mount 只按前缀匹配），而连接器里填的 URL
   不会自动带尾斜杠，部分客户端也不跟随重定向。挂根路径后由
   `BearerGate` 自己判前缀，`/mcp` 与 `/mcp/` 都能直连，其余路径在这里 404。
3. **stateless + JSON 响应。** 有状态会话在 `systemctl restart` 之后全废，
   而连接器不知道要重新 `initialize`，表现是「显示已连接、每次调用都失败」。
   JSON 响应则绕开反代缓冲吃掉 SSE 事件的问题；要推进度事件时再把
   `MCP_INBOUND_JSON_RESPONSE` 关掉并配 `proxy_buffering off`。

另外关掉了 SDK 的 DNS rebinding 保护：默认只放行 `127.0.0.1/localhost` 的
Host，挂在真实域名后面会直接 421。那道保护防的是「浏览器里的恶意页面
拿用户 cookie 打本地端口」——这里不用 cookie，凭据是显式 Bearer 头。

## 6. 上传通道（`POST /mcp/upload`）

「一句话把文件交给 PAP」听起来该是一个工具，实际不能是：**模型搬不了字节**。
MCP 工具的参数是 JSON，文件只能 base64 内联，而一个 169KB 的文档就是二十多万
字符的 base64——调用方模型得逐 token 把它生成出来，既做不到也没人愿意付这个上下文。
所以文件必须由**有字节的进程**搬运，端点是 multipart，不是工具。

```
POST /mcp/upload
Authorization: Bearer pap_xxx
Content-Type: multipart/form-data

file          必填，文件本体
title         可选，缺省用文件名（截断 200 字）
access_scope  可选，internal / restricted / public，缺省 internal
```

响应 200 与 `/api/v1/knowledge/documents` 同构，另带一句 `message`：

```json
{
  "document": {"id": "...", "title": "...", "chunk_count": 333, "status": "ready", ...},
  "action": "created",
  "size_bytes": 173041,
  "message": "已入库《T/CGAS 007-2019 …》：333 片 · 分级 internal。"
}
```

`action` 三值：`created` / `updated` / `unchanged`。**同一份正文重复上传是幂等的**
（按内容 sha256 判定），`unchanged` 不会重复切片——没有这条，同一份标准传两次
就会在检索结果里出现两份、各占一份切片。

### 三个刻意的决定

1. **不是一个 MCP 工具。** 理由上面说了：工具参数装不下字节。工具面仍然只有
   `pap_delegate` + `pap_task_status` 两个。
2. **不挂 `/api/v1`。** `/api/v1` 的身份来自网关注入的头（浏览器面，Basic 认证）。
   把 PAT 接进 `get_current_identity` 等于让一枚令牌获得**全站控制台权限**，
   而 PAT 的设计前提正是「只开一个窄口子」。所以这里复用 `BearerGate`：
   同一枚令牌、同一套身份解析、同一个 `Identity`，但**出口只有这一个端点**。
3. **授权不看令牌，看人。** `knowledge:write` 或平台管理员，与 REST 上传同一个
   判据——两处都调 `app/knowledge/upload.py` 里那一份实现。分成两份的下场是
   「REST 传得进去、MCP 传不进去」，或者更糟：一处做了去重、另一处没做，
   同一份资料在库里变两份。集成测试 `tests/integration/test_mcp_upload.py`
   就是守这条的。

`settings.knowledge_max_file_bytes` 是唯一上限。**以实际读到的字节数为准**，
不信任 multipart 里客户端声明的长度。

命令行在 `scripts/pap_push.py`，它是「一句话交给 PAP」在真实链路里的形态——
模型执行一条命令，字节由进程搬：

```bash
python scripts/pap_push.py docs/某标准.md --url http://127.0.0.1:8080/mcp
```

令牌按 `--token` → `PAP_TOKEN` → `~/.workbuddy/mcp.json` 里 `pap` 那条的
`Authorization` 顺序取，所以配好连接器之后不需要再贴一次令牌。

## 7. 反代

线上整站有 HTTP Basic（`deploy/nginx/personal-agent-platform.conf`），
MCP 端点不能沿用：把 Basic 凭证写进连接器的 `mcp.json` 等于把明文口令
落在用户机器上，还是所有人共用一份。

实际配置已经在仓库里（`location ~ ^/mcp(/|$)`，`auth_basic off`），
三条要点：

1. **用正则，且要匹配到第二段。** 应用同时接受 `/mcp` 与 `/mcp/`，
   而上传端点是 `/mcp/upload`。写成 `location = /mcp` 会让带尾斜杠的请求掉进
   `location /`；写成 `^/mcp/?$` 会让 `/mcp/upload` 掉进去——
   两种都是「被 Basic 挡成 401」，表现是「握手正常、上传 401」，
   比完全不通报错更费解。
2. **显式清空身份头**（`X-Authenticated-Subject` / `-Tenant` 置空）。
   这个端点不接受任何网关注入的身份，只认 Bearer 令牌。
3. **单独一个限流桶** `pap_mcp`（4r/s）。一次委派可能占住连接 30 秒，
   和浏览器控制台共用 `pap_api` 时，几个并发就能把界面刷成 429。
   超时 `proxy_read_timeout` 必须大于 `MCP_INBOUND_WAIT_SECONDS`。

**改动前先确认**：`auth_basic off` 之后应用层鉴权是唯一入口，
所以 `MCP_INBOUND_ENABLED=false` 时这一段不该存在（此时 `/mcp` 会落到
`location /` 上要 Basic，反而是安全的）。

## 8. 自检

```bash
python -m scripts.check_mcp_server                    # 自建 sqlite + 现签一枚 PAT
python -m scripts.check_mcp_server --call "西环站现在什么状态" --token pap_xxx
```

默认模式不碰任何已有环境：起一个临时 sqlite、种一个用户、**当场签一枚 PAT**，
然后断言这些事——无令牌 401、错令牌 401、PAT 200（能看到两个工具）、
工具注解与 `allow_write` 入参存在、上传端点四种结果（首次 `created`、重复上传
`unchanged`、坏后缀 400、GET 405）、**撤销之后立刻 401**。最后一条是 P1 的全部意义，
必须自动化。

`--call` 才连真实环境：它用你给的令牌打一次真实委派，需要库、模型与
真实账号。刻意不复用默认模式的临时库——那会在空库里跑对话，
失败信息全是干扰。

脚本自建一个只挂 MCP 端点的小应用、起在随机端口，用**官方 mcp 客户端**
走真实握手。刻意不用 ASGI 内联：mount、路径前缀、中间件顺序这几处，
只有真的过一遍网络栈才看得出来。

单元测试覆盖状态折叠、返回体、超时移交、鉴权门槛与令牌隔离
（`tests/unit/test_mcp_server.py`、`tests/unit/test_api_tokens.py`）。
上传端点另有一组集成测试（`tests/integration/test_mcp_upload.py`），
重点是「两条传输共用一份入库实现」——它一旦漂移，症状是其中一条传得进去、
另一条不进去，而两边的单测都不会红。

## 9. 接入形态：为什么只能走 `mcp.json`

同一个 `/mcp` 端点，宿主侧有两种接法，但**只有一种现在能用**：

| | `~/.workbuddy/mcp.json` 里的服务 | 连接器插件（目录市场） |
|---|---|---|
| 现在可用 | 是 | **否**（见下） |
| 出现在连接器选择器 | 不会 | 会（如果能装上） |
| `list_installed_plugins` 能查到 | 查不到 | 查得到 |
| 启用/停用 | 改 json 后重连 | 界面上勾选 |
| 凭据 | `headers.Authorization` | 同样 `headers.Authorization` |

**查不到不是装坏了。** 第一形态是「自定义 MCP 服务」，工具直接注入模型工具表，
不经过连接器目录。平台已经在这上面误判过一次，不要再当成故障排查。

### 为什么连接器形态走不通（2026-11-16 实测）

三条证据，都在本机 `~/.workbuddy/` 下：

1. **连接器目录是服务端下发的。** `plugins/known_marketplaces.json` 里
   `workbuddy-connector-plugins-official` 的 `type` 是 `provider`，其
   `.codebuddy-plugin/marketplace.json` 带 `metadata.workbuddyConnectorCatalog`
   （`identityKey` + 每条目的 `plugin_sha256`）。本地有 `type: "directory"` 的市场，
   但那是专家/插件用的（`experts`、`my-experts`），不喂连接器。
2. **手写宿主状态会被拒。** 把自造的市场目录放进 `plugins/marketplaces/` 并手工
   加进 `known_marketplaces.json`，重启后宿主报
   `marketplace/add ... code=INTERNAL_ERROR reason="Error: Plugin authority changed"`，
   市场与插件都不出现——authority 只有宿主自己能写。
3. **选择器的启用状态在别处。** `connectors/<accountId>/connector-states.v3.json`
   里是 `enabled` / `everConnected` / `userDisabled`，凭据在 `headerOverrides`
   里按账号加密（aes-256-gcm，密钥来自 `master.key`）。`pap` 不是连接器，
   永远不会出现在这份名单里。

**结论：不进目录 = 不进选择器。** 要进目录只有官方上架一条路（服务端登记 + 签名），
不是本机能解决的。

`scripts/build_connector_plugin.py` 保留着，产物与官方连接器同构：
宿主哪天开放自定义连接器（或我们自己能把包挂到 provider 目录下）时直接可用。

```bash
python -m scripts.build_connector_plugin \
  --url http://127.0.0.1:8080/mcp --token pap_xxx
```

默认落在 `deploy/connector/`（已 gitignore，**包里有个人令牌**）。

**即使将来装上，凭据也只能写成明文头。** 连接器插件的注入只认 `mcp-oauth` 与
`oneid-token`（见官方包 `tencent-docs` 的 `injectionRules`），没有静态令牌这一档；
但插件 `mcp.json` 的 `headers` 是 schema 明确允许的。所以包只能自己用，
发出去等于把账号发出去——这才是 OAuth 的真实触发条件。

## 10. 没做的

- **OAuth 2.1**（连接器侧 `auth.mode=oauth`）。需要 PAP 当授权服务器 + DCR +
  PKCE + 公网 HTTPS，成本是 PAT 的三到五倍。PAT 就够内部用。
  **它现在只卡一件事**：把连接器包分发给别人。自用形态（令牌写进 `mcp.json` 的
  `headers`）已经能用，见 §9。触发条件是「第二个人要用这个连接器」。
  另一个前置条件是域名与证书——服务器目前只有 80。
- **令牌的 scopes**。一枚令牌等于本人全部权限。按能力收窄要先把
  「MCP 暴露面」定义清楚，而现在对外只有一个委派入口——
  收窄一个粒度粗到没有意义的维度，只会制造「明明配了却调不动」的问题。
  `allow_write` 是这一点上的**半步**：它按次收窄写能力，但收窄的是「这一轮」，
  不是「这枚令牌」。
- **写操作的执行**。MCP 端永远只返回 `awaiting_approval`：审批是图内
  `interrupt()` + 人工确认，而模型完全可以自己调一次「确认」工具——
  那样审批链就变成装饰品。确认必须由人在 PAP 控制台点。
- **进度事件**。JSON 响应下没有中间回执，慢任务靠句柄移交。要做就得同时
  关掉 `mcp_inbound_json_response`（改 SSE）并在反代上 `proxy_buffering off`，
  两处必须一起改，只改一处会变成「请求挂住、没有任何字节回来」。
