# MCP 接入

平台通过 MCP（Model Context Protocol）接入服务器上的燃气调压 MCP Server，
把它的站点查询与控制能力变成受控工具，走与本地工具**完全相同**的链路：
注册表 → Schema 校验 → 角色/Scope → 站点/资源 → 限流 → 审批 → 审计。

## 1. 对端

| 项 | 值 |
|---|---|
| 目录 | `/srv/upstream-mcp`（服务器宿主，**不是 git 仓库**，代码从这里 rsync/构建） |
| 上游仓库 | 本地 `upstream-mcp`，部署版本 `<commit>` |
| 部署方式 | Docker 容器 `prv-mcp`，镜像 `station-mcp:1.1`，`0.0.0.0:8765 -> 8765` |
| 端口收敛 | `deploy/firewall/docker-port-guard.sh` 的 `DOCKER-USER` 规则挡住外部直连（已 `persist`） |
| 端点 | `http://127.0.0.1:8765/mcp`（Streamable HTTP） |
| 鉴权 | `Authorization: Bearer <token>`，令牌表在 `/etc/prv/tenants.json` |
| 鉴权前筛 | `PRV_STRICT_HTTP_AUTH=1`（本机已开），见 §10 |
| 产物目录 | 宿主 `/var/lib/prv/{trends,exports,reports,logs}` → 容器 `/app/*`，重建容器不丢文件 |
| 探针 | `/healthz`、`/readyz`（不经鉴权中间件；`/readyz?probe=1` 需令牌） |
| SDK | `mcp==2.2.0`（平台侧锁同一版本，见下） |

`mcp` 2.x 的客户端 API 与 1.x 完全不同（`streamablehttp_client` → 
`streamable_http_client`，返回 2 元组而不是 3 元组，模型字段是 snake_case）。
所以 `pyproject.toml` 里锁死 `mcp==2.2.0`，与对端 requirements 一致；
跟版本漂移会让接入在某次 `uv sync` 之后突然坏掉。

## 2. 本地映射

远端 22 个工具，代码白名单暴露 **21** 个。全部带 `prv_` 前缀，避免与本地工具重名。

白名单有两个来源，代码优先：

1. `app/tools/mcp_tools.py` 里的 `MCP_TOOL_SPECS`（下表）——带审批语义，
   不允许被配置覆盖，也不能在控制台取消；
2. **控制台接入**（2026-11-05 起）：管理员在「工具 → MCP Tool → 发现」里
   对未接入的远端工具点「接入」，填本地名、中文说明与风险等级，
   存 `mcp_registered_tools` 表并立即热注册，无需重启。
   入参 schema 直接取自远端，本地不编造；写及以上风险强制走审批
   （由 ToolGateway 按风险等级单点推导，配置里没有免审批开关）。
   取消接入会同时删行和从注册表摘掉，远端服务不受影响。

### 2.1 端点跟着接入项走（多 MCP Server）

`mcp_registered_tools` 每条记录自带 `server_url` 与 `token`，运行时按它调用：

| `server_url` | 令牌来源 | 说明 |
|---|---|---|
| 空 / 等于 `MCP_SERVER_URL` | 该租户在 `MCP_TENANT_TOKENS` 里的令牌 | 与 v1 行为一致，库里的 `token` 不参与 |
| 其它地址 | 该行自己的 `token`；为空表示这个端点不鉴权 | 绝不把默认端点的租户令牌发给另一个服务 |

为什么这么改：v1 假设「整个平台只有一条 MCP 通道」，地址只从 `.env` 取。
接第二个服务（本机 OCR MCP，`http://127.0.0.1:8001/mcp`）时，发现是照着表单里
的地址做的，接入却固定拿 `.env` 的地址去校验，于是报
「远端没有名为 ocr_file 的工具，可能已下线或改名」——工具明明在，只是查错了服务。

现在的约定：
- 「发现」的响应带回 `server_url`，前端接入时原样带上，两处必然是同一个端点；
- 接入校验失败时错误里写明查的是哪个端点，不再让人去怀疑远端下线；
- 令牌在库里明文存（与 `model_providers.api_key` 同一条理由），接口只回
  `token_set`，审计只记地址不记令牌；
- 启动注册会按端点分组拉清单：某个端点连不上只影响它自己的工具。

**没有做的事**（别误以为已经支持）：同一会话里跨多个 MCP Server 做故障转移。
按租户区分外部端点凭据、端点连通性巡检这两件已经由**连接器**补上
（见 `docs/CONNECTORS.md`）：端点与认证方式收到连接器里，令牌按租户各存一份，
健康检查按调用者的凭据跑。

> 连接器与本节的关系：本节描述的 `mcp_registered_tools.server_url` / `token`
> 是「工具自带端点」的旧路径，仍然可用，但**一个系统接十个工具要抄十遍端点**。
> 连接器是那层的上一层：工具只记 `connector_id`，端点和凭据都由连接器提供。
> 两者不要混用——一条记录如果挂了连接器，行上的 `server_url` / `token` 会被忽略。


| 本地工具 | 远端工具 | 风险 | 审批 |
|---|---|---|---|
| `prv_server_status` | getServerStatus | read | |
| `prv_list_stations` | listStations | read | |
| `prv_station_profile` | getStationProfile | read | |
| `prv_station_capabilities` | getStationCapabilities | read | |
| `prv_station_overview` | getStationOverview | read | |
| `prv_query_pressure` | queryPressure | read | |
| `prv_cutoff_valve_status` | getCutoffValveStatus | read | |
| `prv_relief_valve` | queryReliefValve | read | |
| `prv_io_alarms` | queryIOAlarms | read | |
| `prv_query_alarms` | queryAlarms | read | |
| `prv_relief_samples` | queryReliefSamples | read | |
| `prv_relief_report` | queryReliefReport | read | |
| `prv_control_config` | getControlConfig | read | |
| `prv_prepare_station_control` | prepareStationControl | read | |
| `prv_cancel_station_control` | cancelStationControl | read | |
| `prv_station_command` | getStationCommand | read | |
| `prv_list_commands` | listStationCommands | read | |
| `prv_pressure_trend` | queryPressureTrend | normal | |
| `prv_export_records` | exportStationRecords | normal | |
| `prv_download_report_pdf` | downloadReportPdf | normal | |
| `prv_apply_station_control` | prepare + execute | external | ✓ |

**只有真实下发（`prv_apply_station_control`）需要审批。** 三个导出类工具
（趋势 CSV、记录导出、放散报表 PDF）确实会在 MCP 容器里落文件
（`trends/`、`exports/`、`reports/`），但它们读的是站点数据、不改变站点
配置也不下发指令，把「查一次趋势」做成要走审批，成本远大于风险，因此
定为 `normal`。

需要注意 `risk_level` 是判定的唯一开关：网关对 `write` 及以上会**无条件**
强制审批（`ToolGateway.requires_approval`），所以只把 `requires_approval`
改成 `False` 是没用的，必须同时降到 `normal`。

代价是这些导出文件会持续堆积，目前没有自动清理。文件都在 MCP 容器的
数据卷里，需要时观察该卷的磁盘占用增长。

**刻意不暴露**：

- `setScenario` —— 只在 `PRV_DEMO_MODE=1` 的演示部署生效，切完会把后续查询
  变成假数据。生产环境暴露它，等于给模型一个「把真实数据换成演示数据」的开关。
- `executeStationControl` —— 它需要的 `preview_id` 由 `prepareStationControl`
  产生且只活 5 分钟。拆成两个工具交给模型保管，跨轮次极易超时或张冠李戴。
  改为由 `prv_apply_station_control` 在审批通过后内部串联，见下节。

## 3. 控制下发为什么合成一个工具

审批流程可能停顿几分钟，而远端预览只活 300 秒。如果让模型先调 prepare 拿到
`preview_id`、再走审批、再调 execute，就会出现「审批单指向一个已过期预览」的窗口。

合成之后：

```
审批通过 → handler 内部 prepare（条件检查，不写设备）
        → 立即 execute(confirm=true)（300 秒内必然完成）
```

prepare 失败就直接返回，**不会调用 execute** —— 这一点有单测覆盖
（`test_apply_stops_when_prepare_fails`）。远端还会拿 `station` / `action`
与预览记录比对，不一致返回 `PREVIEW_MISMATCH`，同样不写设备。

## 4. 授权边界（容易误解，务必看清）

**平台的站点授权表不覆盖 MCP 站点。**

- PAP 的 `station_grants` 用内部站点 ID（`station-001` 这种）；
- MCP 的 `station` 参数是站名或通信序列号（`T002509008`）。

两者不同域。强行映射只会做出「看起来校验了、实际上对不上」的假授权，
所以这些工具**不设** `station_id_argument`，Gateway 不会拿 PAP 的授权表去校验。

真实边界在对端：

| 控制点 | 位置 |
|---|---|
| 能看到哪些站 | 令牌的 `allowed_stations`（`null` = 全部，`[]` = 一个都看不到） |
| 能不能写 | 令牌的 `write_scope`（`none` / `limited` / `full`） |
| 能写哪些动作 | 令牌的 `allowed_actions` |

写入是两道门取「与」：`write_scope` 不是 `none` **且** `allowed_actions` 非空，
`write_enabled` 才为真。只配其中一个仍然是只读——这一点容易看漏，
因为 `prepareStationControl` 照样会 `success=true`，只有
`confirmation_can_execute` 才反映真实可执行性。

当前部署的两个令牌：

| 令牌 | `allowed_stations` | `write_scope` | `allowed_actions` |
|---|---|---|---|
| `owner` | `null`（全站点） | `limited` | `["high_pressure"]` |
| `isolated` | `[]`（空白名单） | `none` | `[]` |

配置在**容器启动时**加载，改完 `tenants.json` 必须 `docker restart prv-mcp`，
否则看到的还是旧权限。

### 令牌轮换（2026-11-04 实操过一次）

两个坑先记住：

1. **令牌在 `tenants.json` 里是字典的键，不是字段值。** 换令牌等于重建整个 `tokens`
   字典，`tenant_id` / `allowed_stations` / `write_scope` 这些值原样带过去。
2. **两侧都要改，且顺序有讲究。** 先改文件不会立刻生效——`prv-mcp` 在启动时读令牌表，
   PAP 在启动时读 `.env`。所以必须**先 `docker restart prv-mcp`，再
   `systemctl restart personal-agent-platform`**，中间有几秒双向 401 的窗口，
   反过来的窗口更长（PAP 已经用新令牌，对端还在认旧的）。

步骤：

```bash
# 1) 备份（.env 必须保持 600）
cp -a /etc/prv/tenants.json /etc/prv/tenants.json.bak.$(date +%Y%m%d-%H%M%S)
cp -a /personal-agent-platform/.env /personal-agent-platform/.env.bak-$(date +%Y%m%d-%H%M%S)

# 2) 每个 tenant_id 生成新令牌，重建 tokens 字典（值不变，只换键）
# 3) 按 tenant_id 映射改 .env 的 MCP_TENANT_TOKENS，只改这一行
#    现网映射：PAP default → owner（全站点、可写高压档）
#              PAP test    → isolated（空白名单、不可写）
#    映射按 tenant_id 查，别按字典顺序猜
# 4) 重启，顺序不能反
docker restart prv-mcp && systemctl restart personal-agent-platform

# 5) 验证三件事
#    新令牌 initialize → 200；旧令牌 → 401 + -32002；
#    真实对话能读到站点（default 10 个，test 应回「空白名单」）
```

改完 `.env` 最好做一次逐行比对确认**只有 `MCP_TENANT_TOKENS` 这一行变了**——
手工编辑 `.env` 时把别的键删掉一行，症状往往是几天后才出现的 401 或模型不可用。

`.env.bak-*` 里除了已失效的旧令牌，还躺着仍在使用的模型 Key，**不要长期留在
`data/` 或项目根目录**；确认新配置稳定后按需清理。

所以要限制某个 PAP 租户的可见范围，正确做法是**给它配一个受限令牌**，
而不是指望 PAP 的站点授权表。租户与令牌一一对应：

```dotenv
MCP_TENANT_TOKENS={"default": "<owner 令牌>", "test": "<isolated 令牌>"}
```

没有配置令牌的租户直接拒绝，**不回落到别的租户令牌**——那等于用别人的身份读数据。

### 空白名单是「空结果」，不是「拒绝」

`allowed_stations: []` 的令牌调 `listStations` 实际返回：

```json
{"success": true, "count": 0, "total_authorized": 10, "visible_to_identity": 0}
```

不是 401/403。这样设计是对的：它把「没被授权任何站」和「服务不可用」分开。
但代价是**平台必须解释这个 0**，否则模型和用户都会读成「站点全离线」。
`app/tools/mcp_tools.py` 的 `_empty_message` 负责把原因写进 message，
`CHAT_SYSTEM_PROMPT` 再要求模型区分「没有数据」和「没有权限」。


## 5. 配置

```dotenv
MCP_ENABLED=true
MCP_SERVER_URL=http://127.0.0.1:8765/mcp
MCP_CONNECT_TIMEOUT_SECONDS=15
MCP_TENANT_TOKENS={"default":"<token>"}
MCP_MAX_RESULT_CHARS=12000
```

- `MCP_ENABLED=false` 时一个 MCP 工具都不会注册，模型不知道它们存在。
- 启动时拉一次工具清单并按白名单注册；对端不可达只记告警、不阻塞启动，
  此时 MCP 工具不可用（模型目录里没有），其余功能正常。
- 远端少了某个工具时只跳过它。**远端多出来的工具不会被自动注册。**

### 5.1 第二个端点：OCR（2026-11-06 起）

燃气调压那套是「远端工具 → 本地工具」的一一映射，OCR 不是。宿主上的
`/data/paddle_ocr/rapidocr_server.py`（FastMCP，`http://127.0.0.1:4499/mcp`，
不鉴权）只暴露一个工具：

| 远端工具 | 入参 | 返回 |
|---|---|---|
| `ocr_file` | `file_path`：服务器绝对路径 | 纯文本；内部异常也是正常返回 `Error: ...` |

**没有把它注册进 `mcp_registered_tools`。** 它由 `app/tools/ocr_tools.py` 直接调
`McpToolClient`，对模型暴露的仍是本地工具 `ocr_document(file_id | file_url)`：

- `ocr_file` 收的是路径，注册给模型等于送一个「读任意文件」的入口；
- 附件句柄 → 路径、租户/会话目录校验、SSRF 白名单、大小上限都在平台侧；
- 工具名与参数不变，权限/审批/审计/观测、历史会话的观察记录都不用跟着改。

配置（与燃气端点完全独立，各配各的）：

```dotenv
OCR_MCP_URL=http://127.0.0.1:4499/mcp
OCR_MCP_TOKEN=            # 留空 = 不鉴权，不发 Authorization 头
OCR_MCP_TIMEOUT_SECONDS=120
OCR_MAX_TEXT_CHARS=40000
```

三点与燃气端点的差别，容易踩：

1. **传路径，所以必须同机**（或共享挂载同一目录）。跨机部署要么改成传 base64
   （要改对端），要么放弃这条路；
2. **不鉴权**。`OCR_MCP_TOKEN` 留空时客户端不会发 `Authorization` 头；
   不要图省事把 `MCP_TENANT_TOKENS` 的令牌填进来——那是发给另一个服务的凭据；
3. **`is_error` 不可靠**。`ocr_file` 把异常 `return` 成字符串，协议层是成功；
   平台认 `Error:` 前缀并转成 `OCR_SERVICE_ERROR`，否则报错会被当成文件内容。

注意别把 `ocr_file` 的名字和燃气端点上同名工具混淆：`admin_registered_map()`
按 `server_url` 过滤，就是为了不把 A 端点的接入状态标到 B 上。

## 6. 结果语义

对端的协议层 `is_error` 恒为 `False`，业务失败体现在 payload 的 `success` 字段。
因此本地映射规则是：

| 远端 payload | 本地结果 |
|---|---|
| `success=true` + `data_source=API` | `success=true`，`verified=true` |
| `success=true` + `data_source=mock` | `success=true`，`verified=false`，message 注明是演示数据 |
| `success=true` + `count=0` 或集合为空 | `success=true`，message 说明「没有记录」；站点目录会进一步说明是权限边界 |
| `success=false` + `error` | `success=false`，`error_code=MCP_TOOL_ERROR` |
| 无 `success` 字段（如 getServerStatus） | 视为成功，但 `verified=false` |

`verified=true` 只表示数据来自真实接口。拿演示数据说「已核验」是谎报。

结果超过 `MCP_MAX_RESULT_CHARS` 会被截断并标记 `truncated`，
message 里提示缩小查询范围——趋势曲线和站点目录确实能上万字符。

喂给用户的是「一句业务结论 + 排过版的数据」，不是原始 JSON。读工具这条
路径**不经过模型**（`execute_read_tool` 直接把摘要当最终答复），所以摘要
必须是人话。`_render_payload` 的规则：

| 形状 | 呈现 |
|---|---|
| 字典列表（站点、记录、曲线、通道） | 逐条编号，`名称（编号）`；超过 20 条说明省略多少 |
| 曲线条目（含 `points`） | `进口压力(kPa)：0 个点（该时间段没有采样数据）`，有极值补 `范围 a~b` |
| 标量列表（通道名、标签） | 一行 `a、b、c`，超过 8 项给总数 |
| 说明类字段（`notes` / `blockers`） | 一条一行 `·`，并成一句没人读得下去 |
| 扁平对象（时间窗、产物目录） | 一行 `键=值，键=值` |
| 深嵌套对象 | `共 N 项明细（结构较深，未展开）`——不给半截 JSON |
| 元数据 / 权限键 | 省略，由 message 用自然语言说明 |

**不要退回到「JSON 截断 300 字」**。趋势查询的 `artifact` 曾经被截成
`"csv_path": nul…`，用户既读不懂，也分不清是数据就这样还是被截掉了。


## 7. 排障

```bash
# 对端活着吗
curl -s http://127.0.0.1:8765/healthz
docker ps --filter name=prv-mcp

# 令牌对不对（401/403 说明令牌错；404 说明路径不是 /mcp）
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -X POST http://127.0.0.1:8765/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'

# 平台注册了多少 MCP 工具
journalctl -u personal-agent-platform -n 200 --no-pager | grep mcp_tools_registered

# 工具是否出现在模型目录
curl -s -H 'X-Authenticated-Subject: pap-admin' http://127.0.0.1:8000/api/v1/tools \
  | python3 -c "import json,sys; print([t['name'] for t in json.load(sys.stdin) if t['name'].startswith('prv_')])"
```

常见错误码：

| error_code | 含义 | 处理 |
|---|---|---|
| `MCP_CREDENTIAL_MISSING` | 该租户在 PAP 侧没配令牌 | 补 `MCP_TENANT_TOKENS` 并重启 |
| `MCP_AUTH_REJECTED` | 令牌被远端拒绝（缺失 / 无效 / 已停用） | 核对令牌是否被吊销、是否属于该租户 |
| `MCP_UNAVAILABLE` | 连不上 / 端点不存在 / 会话被终止 | 查容器状态、`MCP_SERVER_URL` 路径 |
| `MCP_TIMEOUT` | 远端响应超时 | 看对端日志，判断是不是外部平台慢 |
| `MCP_PROTOCOL_ERROR` | 交互不符合协议（含协议版本不兼容、网关拦截） | 看 message 原文；版本问题需升级 SDK |
| `MCP_TOOL_ERROR` | 远端业务拒绝（如 `write_scope=none`） | 看 message 里的 `error` 原文 |

分类按服务端 JSON-RPC 错误码判定（`-32001` 缺令牌、`-32002` 令牌无效），
不看 HTTP 状态。原因是对端在不同灰度阶段会用不同 HTTP 状态承载同一个错误：
鉴权前筛关闭时是 `200 + -32001`，`PRV_STRICT_HTTP_AUTH=1` 后是 `401 + -32001`。
只有错误体里的 `code` 在两个阶段都稳定。

## 8. 联调清单

1. `MCP_ENABLED=true` + 令牌配好后重启，日志应出现 `mcp_tools_registered`，
   `registered=21`。
2. `/api/v1/tools` 里出现 21 个 `prv_*`。
3. `default` 租户问「列出你能看到的调压站」，应返回全部 10 个站点（owner 令牌覆盖全站点）。
4. `test` 租户配 `isolated` 令牌后问同一句，应得到「当前身份看不到任何站点」
   并说明是空白名单——**不是** `MCP_CREDENTIAL_MISSING`，更不能拿到 default 的 10 个站。
   两种隔离形态要分清：没配令牌是「拒绝」，配了空白名单是「空结果」。
5. 发起控制下发，应先进审批。确认后分三种结果：
   - 对端 `write_scope=none` → 「控制预演未通过，未下发任何指令」；
   - 动作不在 `allowed_actions` → 同样中止，`blockers` 说明是哪个门没开；
   - 两道门都放行 → 真正下发，由 handler 内部串联 prepare→execute。
6. 审批通过后如果对端说不能执行，必须停在 `MCP_PRECONDITION_FAILED`，
   不能再调 `execute`——那是把「审批通过」当成「条件满足」。


## 9. 已知限制

- 每次工具调用新建 MCP 会话（多一次 initialize 往返，同机约几十毫秒）。
  没有做连接复用：多 worker 共享连接会引出跨 event loop 的问题，
  在当前调用量下不值得。
- 审批取消/过期时不会回调对端的 `cancelStationControl`。孤儿预览在 300 秒后
  自行过期，无副作用；但因为控制下发是「审批后才 prepare」，正常路径不产生预览。
- MCP 工具的租户策略（`tenant_tool_policies`）与本地工具共用同一张表，
  可以按租户单独停用某个 `prv_*` 工具。
- 控制台接入的工具有几个边界：接入/取消接入不清理 `tenant_tool_policies`
  里按本地名留下的策略行（重新接入同名工具时沿用，是刻意行为）；
  远端工具下线后接入项还在库里，启动时跳过并记 `mcp_tools_missing`，
  页面上显示「未注册」；接入项存的是接入当时的端点，之后改
  `MCP_SERVER_URL` 不会把它们一起改指向（要改就重新接入）。

### 档位动作会顺带改掉 pattern，且不可逆（2026-11-03 实测）

对端所有控制动作都是两步：先 `PUT /depot/curve/{id}` 把 `pattern` 置 1（随机模式），
再 `PUT /depot/press/{id}` 改 `pressType`。第二步之后**没有**任何动作把
`pattern` 改回 2（时钟）或 3（保持）——远端工具清单里也没有设置 pattern 的接口。

实测：T002511111 执行 `high_pressure` 前后

| 字段 | 执行前 | 执行后 |
|---|---|---|
| `pattern` | 3（保持模式） | 1（随机模式） |
| `pressType` | 0（低压） | 65535（高压） |

也就是说，一次「切高压档」实际把站点的调压模式从「保持」变成了「随机」。
随机模式下压力量会随负载波动，这不是用户点确认时预期的结果。

平台的审批预览已经把「先切随机模式，再切档位，两步非事务」写进 `risk`，
但**没有说这一步回不去**。要恢复原状只能直连平台 API（`PUT /depot/curve/{id}`
改 `pattern`、`PUT /depot/press/{id}` 改 `pressType`），或在平台管理端手工改；
MCP 工具清单里没有任何设置 pattern 的接口。

直连平台 API 需要复刻前端 `crypto.js` 的密码加密（AES-128-CBC、iv=key、
ZeroPadding、base64，密钥硬编码在对端 `server.py`），再 `POST {base}/admin/login`
取 Bearer。已实测可回滚：`pattern 2→3`、`pressType 65535→0`，回读一致。

另一个坑：**回读 `pattern` 得到的值不代表稳定状态**。下发后立即读到 1，
几分钟后直连读到 2，说明平台侧会自行轮换模式。判断「是否已回滚」要多等一会儿再读一次。


## 10. 与对端版本的关系（2026-11-04）

对端在后续一版里加了一层 **HTTP 层鉴权前筛**（`prv_auth.AuthPrefilter`），
由 `PRV_STRICT_HTTP_AUTH` 控制，默认关闭。它对客户端的要求只有一条，
但踩不上就会误判：

| 阶段 | 对端行为（同一件事的两种承载） |
|---|---|
| 前筛关闭（默认） | `200` + JSON-RPC 错误体，`code = -32001 / -32002` |
| `PRV_STRICT_HTTP_AUTH=1` | `401` + `WWW-Authenticate` + **同样的** JSON-RPC 错误体 |

所以客户端**不能按 HTTP 状态判断鉴权失败**，因为同一个「令牌无效」在两个灰度
阶段分别是 `200` 和 `401`。唯一稳定的判据是错误体里的 `code`。
`mcp_client._classify` 就是按这个写的：先看 `code`，拿不到再看状态码。

另外三项变更对客户端无影响，但排障时会遇到：

- **协议版本先于鉴权校验**：`MCP-Protocol-Version` 不在服务端支持集合内会返回
  `400`（不是 401）。客户端（`mcp==2.2.0`）的发法是协商后的版本，正常不会撞；
  撞上说明两边 SDK 版本差距过大，报错会直接说明这一点。
- **`GET` / `DELETE` 也要令牌**：前筛覆盖全部方法。SDK 的 SSE 监听与
  `DELETE` 终止会话走的是同一个带 `Authorization` 的 client，实测通过。
- **挂了 EventStore**：`Last-Event-ID` 续传可用。PAP 是短会话（一次调用一建一关），
  用不到续传，但 SDK 的 `GET` 监听不再 500。

验证方式（对端开严格模式后跑一遍）：

```bash
# 好令牌：应返回 22 个工具
# 坏令牌：应得到 MCP_AUTH_REJECTED + 「令牌无效或已停用。」
# 空令牌：应在构造阶段就被拒绝，不会发出请求
```

实测对比（同一台机器、同一对端）：

| 客户端 | 坏令牌的结果 |
|---|---|
| 旧版 | `MCP_PROTOCOL_ERROR` / `MCP 交互失败：MCPError: 令牌无效或已停用。` |
| 当前 | `MCP_AUTH_REJECTED` / `令牌无效或已停用。` |

差异的意义在排障方向：前者会让人去看网络和 URL，后者直接指向凭据。

### 2026-11-04 已上线（镜像 `1.1`，前筛开启）

改动只有四个文件：`prv_auth.py`、`prv_runtime.py`、`serve.py` 更新，`prv_event_store.py` 新增；
`server.py`、`Dockerfile`、`requirements.txt` 与旧版一致（构建前逐文件比对过 md5）。

容器环境加了 `PRV_STRICT_HTTP_AUTH=1`，`/readyz` 回 `"strict_http_auth": true`，
启动横幅打印「HTTP 鉴权：严格（ASGI 前筛，全部方法 401）」。

实测（同一台机器、生产端口 8765）：

| 请求 | 旧版 | 新版 |
|---|---|---|
| 无令牌 `GET /mcp` | 200 + 建立 SSE 流 | `401` + `WWW-Authenticate: Bearer realm="station-mcp", error="invalid_request"` + `-32001` |
| 无令牌 `DELETE /mcp` | 200（可终止他人会话） | `401` + `-32001` |
| 坏令牌 `initialize` | 200 + `-32002` | `401` + `-32002`（`data.transport=http-prefilter`） |
| 非法 `MCP-Protocol-Version` + `GET` | 405 | `400`（规范要求） |
| 好令牌 `initialize` | 200 | 200，协议 `2025-06-18` |

PAP 侧不需要改代码：`_classify` 本来就先看 `code` 再看状态码。端到端复核走真实对话
（「查 T002509008 进口压力」→ 1.538 kPa），应用日志里该轮 `POST /mcp` 202/200、
`GET /mcp` 200、`DELETE /mcp` 200，说明严格模式下 SDK 的 SSE 监听与会话终止都带上了令牌。

**重建容器会丢产物**，所以这一版把 `trends/exports/reports/logs` 从容器层挪到宿主
`/var/lib/prv/`（属主 `10001:10001`，与镜像内 `prv` 用户一致）。旧文件用
`docker cp prv-mcp:/app/trends/. <目录>` 迁移过，`logs/` 里的审计 jsonl 也一并搬了。

重建命令（供下次照抄，注意两个挂载源文件在 `/etc/prv/`，不要打进镜像）：

```bash
docker build -t station-mcp:1.1 /srv/upstream-mcp
docker stop prv-mcp && docker rename prv-mcp prv-mcp-old      # 留作回滚
docker run -d --name prv-mcp --restart unless-stopped -p 8765:8765 \
  -e PRV_ALLOWED_HOSTS="203.0.113.10,203.0.113.10:8765,127.0.0.1:*,localhost:*" \
  -e PRV_STRICT_HTTP_AUTH=1 \
  -v /etc/prv/tenants.json:/app/tenants.json:ro \
  -v /etc/prv/config.json:/app/config.json:ro \
  -v /var/lib/prv/trends:/app/trends -v /var/lib/prv/exports:/app/exports \
  -v /var/lib/prv/reports:/app/reports -v /var/lib/prv/logs:/app/logs \
  station-mcp:1.1
```

回滚：`docker rm -f prv-mcp && docker rename prv-mcp-old prv-mcp && docker start prv-mcp`
（旧容器是镜像 `1.0`、前筛关闭，`/etc/prv/*.json` 与它无关）。

注意 `/srv/upstream-mcp` 不是 git 仓库，**服务器上没有版本记录**。下次同步要么重新
`scp` 上述文件，要么把它改成 `git clone` 的检出；否则「线上跑的是哪个 commit」只能靠比对 md5。
