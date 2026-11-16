# 文字模型服务（Model Provider）

## 1. 这套东西解决什么

改造前模型只由 `.env` 的三个键决定（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`），
改模型必须改文件并重启进程，控制台也无法回答「现在到底在用哪个模型」。

现在模型供应商存在 `model_providers` 表，由平台管理员在控制台
「运行配置 → 模型供应商」维护。切换供应商**不需要重启**，下一条消息即生效。

普通聊天、工具调用、图片理解（多模态 OCR）三条路径共用这里选中的模型。

## 2. 解析优先级

```
model_providers 中 is_active = true 的那一条
        ↓ 没有（一条都没配 / 都被删了）
.env 的 OPENAI_* 兜底
```

生效结果由 `GET /api/v1/admin/model-providers` 的 `effective` 字段直接给出，
页面不自行推断。

## 3. 表结构与约束

| 列 | 说明 |
|---|---|
| `name` | 供应商名称，唯一 |
| `protocol` | `openai-compatible` / `openai` |
| `api_key` | 明文存库，接口只回 `api_key_set` |
| `base_url` | 留空表示用供应商默认地址 |
| `models` | `[{"id": "...", "kind": "text"|"image"}]` |
| `default_text_model` | 对话实际使用的模型名 |
| `default_image_model` | 仅登记，平台无图片生成能力 |
| `temperature` | 0–2 |
| `reasoning_mode` | `auto` / `low` / `medium` / `high`（只管对话回复，见 §7） |
| `is_active` | 同一时刻最多一条为 true |

`is_active` 在 PostgreSQL 上由部分唯一索引 `uq_model_providers_single_active`
兜底，避免并发激活出现「两个都在用」。SQLite 不建该索引（不支持 `WHERE`），
退化为应用层先清后设。

## 4. 安全边界：不跨供应商复用密钥

供应商填了 `base_url` 却没填 `api_key` 时，**不会**回落到 `.env` 的
`OPENAI_API_KEY`。否则给一个指向第三方地址的供应商留空密钥，就等于把
DeepSeek 的密钥连同请求一起发给第三方。这种情况直接判定为「未配置」，
页面上标 `缺密钥` 并提示对话会失败。

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/model-providers` | 列表 + 当前生效模型 |
| POST | `/api/v1/admin/model-providers` | 新建（首个自动生效） |
| PUT | `/api/v1/admin/model-providers/{id}` | 局部更新，`api_key: ""` 表示清空 |
| POST | `/api/v1/admin/model-providers/{id}/activate` | 设为使用中 |
| DELETE | `/api/v1/admin/model-providers/{id}` | 删除；若删的是生效项，自动提升剩余最早一条 |
| POST | `/api/v1/admin/model-providers/discover` | 只读探测 `GET {base_url}/models` |

全部要求 `platform_admin`，写操作落 `ADMIN_ACTION` 审计（不记密钥）。

## 6. 操作步骤

1. 控制台 → 运行配置 →「添加供应商」
2. 填名称、协议、API Key、Base URL
3. 点「发现模型」拉取真实模型列表，或手填模型 ID
4. 在「文字模型」里确认默认模型（不选时自动取列表里第一个文字模型）
5. 保存。第一条供应商自动设为使用中

## 7. 思考模式与结构化规划

这是接 DeepSeek 时最容易踩的一个坑，值得单独说。

规划器要把模型输出变成结构化对象，用的是 `with_structured_output(method="function_calling")`，
它会给请求加上 `tool_choice=函数名`（强制模型必须调用这个工具）。而思考模式模型
明确拒绝这种组合：

```
400 Thinking mode does not support this tool_choice
```

`deepseek-flash` 与 `deepseek-v4-pro` 都是思考模式，所以**不处理就一条消息都发不出去**。
实测只有两条出路：关掉思考（`reasoning_effort="none"` 或 `thinking.disabled`），
或者放弃强制 tool_choice（模型可能只回文本，解析不到结构化结果）。

规划任务的输出只是一段很短的 JSON，本来也不需要长思考，所以平台的处理是：

- **规划器强制关思考**，与供应商的 `reasoning_mode` 无关；
- 供应商不认 `reasoning_effort`（多数非推理模型直接 400 `unsupported parameter`）时，
  自动去掉该参数重试一次，并记住这份配置，后续不再先失败一次；
- 万一遇到「连关都关不掉」的供应商，报错文案会直接说清原因和改法，
  而不是笼统的「模型服务暂时不可用」。

`reasoning_mode` 只作用于对话回复路径，不影响规划器。

## 8. 已知限制

- **图片生成模型只登记**：平台没有图片生成链路，这一组不参与任何调用，
  页面上已明确标注。
- **思考模式依赖供应商**：`low/medium/high` 会作为 `reasoning_effort` 下发，
  实测 `api.deepseek.com` 接受该字段并返回 `reasoning_content`；
  部分兼容服务会忽略未知字段，少数会 400。不确定就保持 `auto`。
- **模型名以 `GET /models` 为准**：例如 `api.deepseek.com` 只接受
  `deepseek-flash` 与 `deepseek-v4-pro`；`deepseek-chat` 是别名，实际路由到
  `deepseek-flash`；`deepseek-v4.1-flash` 不存在，会 400。
- **图片理解受供应商能力约束**：云端识别直接用当前生效供应商的模型，能否看懂图
  取决于该模型是否支持视觉。实测 `deepseek-flash` 接受图文混合请求（返回 200），
  但识别质量需按实际结果评估；不满意就把 OCR 配置切回本地 RapidOCR。
