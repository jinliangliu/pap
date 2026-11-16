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

## 8. 已知限制

- 不支持 multipart / 文件上传类接口；
- 不支持 OAuth2 客户端凭证等需要换取 token 的流程（只能用静态 Bearer）；
- 不支持自定义响应映射：2xx 一律视为调用成功，业务成败由模型读 `data` 判断；
- 自定义工具不支持按租户配置不同 URL：注册表是全局的，租户只决定「能不能用」。
