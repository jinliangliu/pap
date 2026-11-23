# 工具：来源、自定义 HTTP Tool 与导入导出

工具页展示的是一个**唯一注册表**（`ToolRegistry`），模型能看到什么、
能调用什么，全部由这里决定。控制台只是这层注册表的读写界面，不是另一套配置。

## 1. 三类工具与来源

| kind | source | 谁创建 | 能不能在页面改 |
|---|---|---|---|
| `http` | `builtin` | 代码注册（`app/tools/*_tools.py`） | 只读 |
| `http` | `custom` | 控制台「新建」 | 可改、可停用、可删除、可导入导出 |
| `mcp` | `mcp` | 远端 MCP 白名单（`app/tools/mcp_tools.py`） | 只读；可「发现」远端清单 |
| `python` | `builtin` | 代码注册（沙箱 / OCR / 演示工具） | 只读 |

`source` 字段由 `/api/v1/tools` 返回。它不是装饰：详情页的「能不能编辑」
和「同名冲突」的判断都依赖它，不要用 `kind` 猜。

## 2. 自定义 HTTP Tool 的数据模型

表 `custom_tools`（平台级，不挂 RLS）：

| 字段 | 说明 |
|---|---|
| `name` | 工具标识，`^[a-z][a-z0-9_]{1,63}$`，唯一，保存后不可改 |
| `display_name` / `description` | 展示名与给模型看的说明 |
| `method` | GET / POST / PUT / PATCH / DELETE |
| `url` | 只支持 http(s)，路径占位符写 `{id}` 或 `:id` |
| `risk_level` | `read` / `normal` / `write` / `execute` / `destructive` / `external` |
| `timeout_seconds` | 1–120，同时作为 Gateway 的 `asyncio.wait_for` 上限 |
| `params` | 参数定义数组，见下 |
| `headers` | 静态请求头（禁止 Authorization / Cookie / Host 等） |
| `enabled` | 停用即从注册表注销 |

参数定义：

```json
{
  "name": "station_id",
  "location": "path",        // path | query | body | header
  "type": "string",          // string | integer | number | boolean
  "required": true,
  "description": "站点标识",
  "enum": []
}
```

- `path`：替换 URL 里的 `{station_id}` / `:station_id`；
- `query`：拼进查询串；
- `body`：合并成 JSON 请求体；
- `header`：作为请求头，只用于非敏感静态值。

URL 里的占位符与实际 `path` 参数必须**双向一致**：声明了 path 参数却没占位符、
或有占位符却没声明参数，保存时都会 400。后者不拦的话，请求会带着字面
`{id}` 发出去，远端返回 404，排查起来像是「接口不存在」。

## 3. 风险等级与审批

审批不是工具自己声明，而是网关按风险等级推导：

```python
_WRITE_RISKS = {WRITE, EXECUTE, DESTRUCTIVE, EXTERNAL}
requires_approval = tool.requires_approval or risk_level in _WRITE_RISKS
```

所以把工具配成 `write` 就一定会走「预览 → 确认 → 执行」，
配置里没有单独的「需要审批」开关，避免两处真相打架。

## 4. 安全边界

| 风险 | 处理 |
|---|---|
| URL 内嵌凭据（`https://user:pass@…`） | 拒绝保存：会被日志、审计、错误信息带出去 |
| 指向云元数据 / 链路本地地址 | 拒绝保存（`169.254.0.0/16` 等） |
| `Authorization` / `Cookie` 静态头 | 拒绝保存，改用租户工具策略的 `credential_ref` |
| 密钥 | 存环境变量，执行时由 Gateway 解析成 `context.credential`，按 Bearer 透传 |
| 重定向 | 不跟随，3xx 按失败处理，避免跳到未授权主机 |
| 响应体过大 | 截断到 12000 字符并标 `truncated` |
| 重试 | 只有 GET 才标 `retryable`；写操作 502/503 也不重试，避免重复提交 |

`credential_ref` 的配置路径：`PUT /api/v1/admin/tenants/{tenant_id}/tools/{tool_name}`，
填的是**环境变量名**，不是密钥本身。

## 5. 注册时机

| 时机 | 行为 |
|---|---|
| 进程启动 | `load_custom_tools()` 读取 `enabled=true` 的行并注册；单条坏配置只跳过那一条 |
| 进程启动 | `load_connector_tools()` 在此之后跑，按连接器分组拉一次远端清单注册 MCP 工具 |
| 新建 / 更新 | 保存成功后立即 `register_tool(replace=True)`，无需重启 |
| 停用 | `unregister_tool()`，模型目录里立刻消失 |
| 删除 | 先从库里删，再注销注册表 |

工具行上的 `connector_id` 非空时，端点与凭据都来自连接器（行上的 `url` 只当相对路径用）。
连接器停用后它下面的工具一律从目录消失，判定读的是注册时的 `connector_ready` 快照——
目录生成是同步路径，拿不到库。细节见 [`CONNECTORS.md`](CONNECTORS.md)。

停用的工具不会出现在 `/api/v1/tools`（网关已注销），但控制台的 HTTP 列表
会用管理接口把「已停用的自定义工具」补回来——否则「停用」就变成单向操作。

## 6. 导入 / 导出

导出文件（`GET /api/v1/admin/tools/export`）：

```json
{
  "version": 1,
  "exported_at": "2026-11-04T07:35:55Z",
  "tools": [
    {
      "name": "work_order_query",
      "display_name": "工单查询",
      "description": "按站点查询工单",
      "method": "GET",
      "url": "https://api.example.com/stations/{station_id}/orders",
      "risk_level": "read",
      "timeout_seconds": 15,
      "params": [{"name": "station_id", "location": "path", "type": "string", "required": true, "description": "", "enum": []}],
      "headers": {},
      "enabled": true
    }
  ]
}
```

导入（`POST /api/v1/admin/tools/import`）：

```json
{"tools": [...], "mode": "merge"}
```

- `merge`：只新增缺失项，同名跳过；
- `overwrite`：同名覆盖；
- 与内置 / MCP 工具同名的条目一律跳过，并给出行级原因；
- 单条失败不影响其余条目，返回 `items[]` 逐条结果。

导出**只包含自定义工具**，不包含内置与 MCP 工具：把代码注册的实现导出成
一份「看起来能再导入」的文件只会误导人。导出文件里也不含任何凭据。

## 7. 「从 URL 解析」与「粘贴 curl 解析」

- `POST /api/v1/admin/tools/parse-url`：把 URL 里的 `{x}` / `:x` 反解成 path 参数，
  并把 `:x` 统一改写成 `{x}`。
- `POST /api/v1/admin/tools/parse-curl`：解析 method / URL / 请求头 / JSON body 参数。
  不执行命令；遇到 `-u` / `--cookie` 直接 400；`Authorization` / `Cookie` 头会被丢弃，
  并在响应里通过 `dropped_headers` 明确告知。

## 8. 一次调用的三段轨迹：决策 → 备份 → 结果

执行入口（`execute_read_only` / `execute_approved`）之后是固定顺序，不能换：

```
决策（写审计、进日志）
  → 写前备份（仅对声明了 backup 钩子的写类工具）
  → handler（带超时）
  → 结果（写审计，带 decision 与 policy_revision）
```

### 8.1 审计记「决策」，不记「命令跑成功」

| 事件 | 何时写 | 回答什么 |
|---|---|---|
| `tool_allowed` | 放行后、执行前 | 谁允许了、依据哪一版策略、备份规则是什么 |
| `tool_denied` | 拒绝时（原有） | 哪一层边界挡住了 |
| `tool_succeeded` / `tool_failed` | 执行后 | 成了没 |

原先只有最后一类。进程在 handler 里被杀掉、或者备份失败提前抛异常时，
审计里什么都不剩——而「批过了、要开始做、然后没了」恰恰最需要事后看见。
决策行带 `success=true`（决策成立），**不表示操作成功**；操作成败在
随后那条事件上，两者用 `decision` + `approval_id` 对齐。

### 8.2 写前备份（`allow_with_backup`）

工具声明 `backup` 钩子（`async (context, arguments) -> BackupSpec | None`）后，
写操作被放行时会先把将被覆盖的状态存进 `write_backups`。

- **钩子返回 `None` 表示「没有旧状态」**（新建），不是失败。混成一种，
  新建类写操作会因为「备不了」被拦下。
- **备份失败 = 不写**。备不下来还照写，等于把「有备份」这句话变成空话；
  抛 `ToolBackupFailedError`，审批记为失败，人能看到「没执行，因为备份没成功」。
- 决策标签 `allow_with_backup` 看的是**规则**（声明了钩子 + 开关开着），
  不是这一次落成了没有。落成与否记在结果事件上。

三个运行时开关（`/api/v1/admin/runtime-settings`，默认 `true / 100 / 3000`）：

| 键 | 语义 |
|---|---|
| `tool.backup_enabled` | 总闸。关掉后钩子不再执行，写照常 |
| `tool.backup_threshold_mb` | **单个对象**超过就不备（一次写不该先灌几百 MB 进库） |
| `tool.backup_max_size_mb` | 该租户备份**总量**上限。到了就停，**不覆盖最旧的一份** |

存满时覆盖旧备份能让上限看起来「永远够用」，代价是早期那次操作的原件在某天
无声消失——而人正是靠它回滚的。日志里三个字段必须一起看：
`backup_enabled=true` 单独出现会让人以为「这次一定备了」，实际可能是阈值挡的。

目前接入了 `studio_workflow_save`：它的 `definition` 是整张图、覆盖即无旧版
（修订号只加一，不留历史），是这套机制最需要的地方。

审计里读这几个字段：

| 字段 | 落在哪条事件 | 说明 |
|---|---|---|
| `backup_hook` | 决策 + 结果 | 这个工具**有没有声明**钩子。`captured` 为空时靠它区分「没钩子」与「有钩子但这次没旧状态」 |
| `backup_enabled` / `_threshold_mb` / `_max_size_mb` | 决策 + 结果 | 当时的规则 |
| `backup_captured` / `backup_skipped` / `backup_size_bytes` | 只在结果 | 这次落成了没有、多大 |
| `backup_id` | 只在结果 | 原件在 `write_backups` 的哪一行。备份发生在决策之后，决策行给不出这个值 |

### 8.3 策略版本（`policy_revision`）

`platform_settings` 每次**生效值真的变了**就 +1 一个单调号，并落一份完整快照
（`policy_snapshots`）。工具上下文带 `policy_revision`，两条审计都记它。

- 为什么是整份快照：要能按号**原样取回**。存增量就得从头重放，而重放依赖
  每一版都在——一次迁移、一次回滚就断了，断在哪一版事后看不出来。
- 为什么 0 不落库：0 表示「从未改过配置，全按 `.env` 默认」，由代码合成。
  空库环境不需要在启动时写一行才能自洽。
- `GET /api/v1/admin/runtime-settings/snapshots/{revision}` 按号取回；
  `found=false` 而不是 404——「这一版不存在」与「接口不存在」处置不同。

沙箱侧同一份会随 `POST /v1/execute` 推过去并要求回显，见 `docs/SANDBOX.md §4`。

## 9. 已知限制

- 不支持 multipart / 文件上传类接口；
- 不支持 OAuth2 客户端凭证等需要换取 token 的流程（只能用静态 Bearer）；
- 不支持自定义响应映射：2xx 一律视为调用成功，业务成败由模型读 `data` 判断；
- 自定义工具不支持按租户配置不同 URL：注册表是全局的，租户只决定「能不能用」。
- **备份不可自动回滚**：`write_backups` 存的是原件，怎么用它恢复由人决定。
  写得进外部系统的那些（连接器 / MCP）根本没有跨系统回滚，这张表只承诺
  「原件还在」。
