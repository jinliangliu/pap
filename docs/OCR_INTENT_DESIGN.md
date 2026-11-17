# 附件 OCR（图片 / PDF）：从「硬编码节点」改为「模型自主调用的工具」

> 状态：已落地（2026-11-04），2026-11-05 扩展到 PDF，
> 2026-11-06 识别改走 OCR 服务的 **MCP** 接口（平台内不再有 OCR 服务配置）。
> 本文取代上一版「ingest_media 固定节点」方案。
> 相关文件：`app/tools/ocr_tools.py`、`app/integrations/mcp_client.py`、
> `app/services/attachment_store.py`、`app/agents/{nodes,graph,prompts,state}.py`

## 1. 为什么推翻上一版方案

上一版把 OCR 做成图里的固定节点：

```
START → ingest_media（无条件 OCR）→ plan_request → ...
```

三个问题：

1. **无条件付成本**。只要请求里带了图，不管用户其实想问别的，都会先跑一次 OCR。
   一张 4 MB 的现场记录表，本地服务要 6 秒——纯文本对话也被拖慢。
2. **意图判断被前置**。OCR 文本是一股脑塞进规划器的，模型无法选择「这张图要不要读、读哪部分」。
3. **不可组合**。节点只发生在规划之前，模型若在执行中才发现需要看另一张图，没有补救路径。

改成工具后，OCR 变成和 `get_station_snapshot` 同级的受控能力，由模型在推理过程中自己决定何时调用。

## 2. 现在的结构

```
START
  → plan_request ──────────────┐
      ├ chat/clarify/error → respond
      ├ read_tool → execute_read_tool ─┬ 还有步数 → plan_request（回路）
      │                                └ 收敛     → respond
      ├ knowledge → retrieve_knowledge → respond
      └ write_tool → create_and_wait_approval → respond
  → persist_state → END
```

两个新增物：

- **`ocr_document` 工具**：注册进 `ToolGateway`，和业务工具一样出现在模型工具目录里。
- **推理回路**：只读工具执行完不再直接收尾，结果作为「观察」回灌给规划器，
  模型据此决定「继续调用下一个工具」还是「直接作答」。

这条回路是本次改造的核心。没有它，OCR 只能单点问答；有了它，才可能
「识别图片里的站点编号 → 用编号查压力趋势」，或「读 PDF 报表里的站点 → 批量查快照」。

## 3. 附件来源：句柄而非内容

模型是纯文本的，看不到附件。所以：

1. `POST /api/v1/chat` 的 `attachments[].data_url`（base64 内联，图片或 PDF）由 `chat_service` 落盘：
   `{OCR_ATTACHMENT_DIR}/{tenant}/{conversation}/{attachment_id}.{ext}`，
   句柄前缀区分类型（`img_` / `doc_`）；
2. 落盘时按**文件头**校验真实类型，声明类型与内容不符以内容为准
   （把 PDF 谎称 `image/png` 进来，仍会按 PDF 处理）；
3. state 里只写 `{"attachment_id", "kind", "mime", "size", "name"}`，**不写 base64**——否则每次
   checkpoint 恢复都要反序列化几 MB 数据；
4. 用户消息末尾追加附件清单，模型因此知道「有几个附件、各自是什么类型、句柄是什么」：

```
[附件]
- file_id=img_9f2c1a4b7e60 类型=图片(image/jpeg) 大小=1234.5KB 文件名=现场.png
- file_id=doc_7c1e04ab93df 类型=PDF(application/pdf) 大小=812.3KB 文件名=日报.pdf

如需读取附件中的文字，调用 ocr_document 工具，file_id 原样使用上面的值。
```

一条消息最多 6 个附件、原始字节合计 40 MB（`CHAT_MAX_ATTACHMENTS` /
`CHAT_MAX_ATTACHMENT_BYTES`）。前端在选文件时就按同样的上限拦截，
避免「选得上、发不出」。

## 4. 工具契约

```python
ocr_document(file_id: str | None, file_url: str | None) -> ToolExecutionResult
```

- 二者必须且只能提供一个；
- `kind="http"`、`risk_level=READ`、`requires_approval=False`、`timeout_seconds=120`；
- **不接受本地路径**，只接受服务端生成的 `file_id`（正则 `^(img|doc)_[0-9a-f]{12}$`）
  或公网 URL——模型无法构造路径读取服务器文件；
- `file_url` 走 SSRF 防护：DNS 解析后若落私网/环回/链路本地/保留地址，直接拒绝。

早期版本叫 `ocr_image`、只收 `image_id`。加入 PDF 后必须改名：
叫 image 却能吞 PDF，模型会按名字判断用途，进而漏调。

返回：

```json
{"file_id": "doc_...", "kind": "pdf", "mime": "application/pdf",
 "provider": "ocr_mcp", "text": "...", "line_count": 149}
```

没有 `page_count`：MCP 的 `ocr_file` 只回一段纯文本，页边界在服务端拼接时丢掉了。
真需要页级定位的文件要先拆分。`truncated=true` 表示超过 `OCR_MAX_TEXT_CHARS`
被截断（文本里会写明原长度与上限）。

失败分类（`error_code`）：`INVALID_ARGUMENTS` / `ATTACHMENT_NOT_AVAILABLE` /
`ATTACHMENT_URL_REJECTED` / `OCR_NOT_CONFIGURED`（没配地址或连不上，重试无用）/
`OCR_SERVICE_ERROR`（可重试）。

远端把自己的异常也当正常文本返回（`Error: ...`）。平台在 `ocr_tools` 里认这个前缀
并转成失败——否则「PDF support not installed」会被当成文件内容念给用户。

「识别不出文字」不算失败：扫描件空白页是正常结果，记成失败会触发重试风暴。

## 5. OCR 服务适配（MCP）

识别不在平台进程里，也没有平台侧的配置表：「运行配置 → OCR 服务」那张卡片
（provider / base_url / 模型 / 启用开关）已删除。现在只有一条路——

```
ocr_document(file_id) ──解析句柄──> /data/uploads/{tenant}/{conv}/img_xxx.png
                     └─ MCP call_tool("ocr_file", {file_path}) ─> 文本
```

对端是宿主上的 `rapidocr_server.py`（FastMCP，`http://127.0.0.1:4499/mcp`，
不鉴权），只暴露一个工具：

| 远端工具 | 入参 | 返回 |
|---|---|---|
| `ocr_file` | `file_path`：服务器上的**绝对路径** | 纯文本；失败时返回 `Error: ...` 字符串 |

平台这层包装保留 `file_id` 契约的理由：

1. 模型不该知道服务器路径。直接把 `ocr_file` 注册给模型，等于给了一个
   「读任意文件」的入口——OCR 服务那边没有任何目录约束；
2. 句柄 → 路径的解析、租户/会话目录校验、SSRF 白名单、大小上限、旧句柄识别
   都在平台这侧，权限/审批/审计/观测也就不用跟着改；
3. 工具名与参数不变，历史会话里的观察记录仍然认得出来。

代价：**OCR 服务必须与平台同机**（或共享挂载同一目录），因为传的是路径不是字节。
`file_url` 会先下载到临时文件（后缀按嗅探出的真实类型给，OCR 服务按扩展名分流引擎），
调用完立刻删除。

配置（`.env`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OCR_MCP_URL` | `http://127.0.0.1:4499/mcp` | 为空 = 没接，调用直接报 `OCR_NOT_CONFIGURED` |
| `OCR_MCP_TOKEN` | 空 | 该服务不鉴权；留空表示不发 `Authorization` 头 |
| `OCR_MCP_TIMEOUT_SECONDS` | 120 | PDF 逐页渲染，随页数线性增长 |
| `OCR_MAX_TEXT_CHARS` | 40000 | 回填给模型的文本上限，超出截断并注明 |

没有「启用 OCR」开关：地址为空就是没接，省掉一个「页面显示启用、实际连不上」
的状态。OCR 是模型自主选用的工具（`ocr_document`），不是流程节点。

## 6. 时限与层叠关系

三层超时必须满足 `nginx < 后端总时限` 且 `后端总时限 > OCR 上限`：

| 层 | 默认值 | 说明 |
|---|---|---|
| `OCR_MCP_TIMEOUT_SECONDS` | 120s | 单次识别调用（PDF 逐页渲染，随页数线性增长） |
| `CHAT_TIMEOUT_SECONDS` | 160s | 整个请求的总闸，超时后后端自己收尾并补一条 assistant 消息 |
| nginx `proxy_read_timeout` | 240s | 必须大于总闸；否则后端明明会回「已中止」，用户只看到 504 |

早期把总闸也设成 120s，与 PDF 上限相等——大 PDF 必然死在总闸上，
识别工具自己的超时形同虚设。

## 7. 收敛与自循环防护

| 机制 | 位置 | 作用 |
|---|---|---|
| `AGENT_MAX_TOOL_STEPS`（默认 3） | `route_after_read_tool` | 步数用尽强制作答 |
| 失败不重试 | `route_after_read_tool` | 工具失败直接作答，不放大故障 |
| 同参重放短路 | `plan_request` | 同一工具同一参数紧接重放，直接用上轮结果收敛 |
| 兜底作答 | `respond` | 回路跑满仍无答复时用最后一次工具结果作答，不吞结论 |

## 8. 代价

**简单工具查询多一次模型调用。** 以前 `plan(tool) → execute → respond` 只调一次模型；
现在是 `plan(tool) → execute → plan(answer) → respond`，多一轮 LLM。

这是「模型自主决定」的直接成本，无法既省调用又保留自主性。
若不接受，把 `AGENT_MAX_TOOL_STEPS` 设为 `1` 即回到旧行为
（工具结果由 `respond` 直接呈现，模型不再参与收尾）。

实测参考：本地 RapidOCR 处理 4.1 MB 现场记录表（149 行文本）约 6.1 秒；
一次工具调用 + 两轮 deepseek-chat，端到端约 8–10 秒。

## 9. 部署清单

1. **迁移**：`alembic upgrade head`（新增的那条会 `DROP TABLE ocr_settings`；
   平台不再保存 OCR 配置，旧表留着只会让人以为还能在页面上改）；
2. **环境变量**：`OCR_MCP_URL`、`OCR_MCP_TIMEOUT_SECONDS`、`AGENT_MAX_TOOL_STEPS`；
   旧的 `OCR_SERVICE_URL` / `OCR_SERVICE_TIMEOUT_SECONDS` / `VISION_*` 已无读取方，可删；
3. **控制台**：没有 OCR 配置项了。`ocr_file` **不出现在**「工具 → MCP Tool」里
   （它是被 `ocr_document` 内部调用的，不是注册给模型的工具），所以别去那里找。
   验通道：打一张图看 `ocr_document` 是否返回文本，或 `curl -X POST {OCR_MCP_URL}`
   看到 400/406（缺 MCP 头）说明端点活着；日志关键词 `ocr_unavailable` / `ocr_failed`；
4. **OCR 服务监听**：实测（2026-11-04 复核，2026-11-06 仍如此）服务绑的是 `127.0.0.1:4499`
   （`ss -lntp` 只有回环地址，从公网 IP 探测返回 000），公网打不通，无需额外收敛。
   早期文档写的「绑 `0.0.0.0`、公网可直达」与现状不符——当时看到的「外部 IP 访问记录」
   是经 SSH 隧道转发进来的，隧道的发起端在公网，落点仍是回环。
   换机器重部署时仍要确认这一点：容器或进程一旦写成 `--host 0.0.0.0`，
   4499 就是无鉴权的公网入口。

## 10. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_ocr_document.py` | 句柄解析、越权句柄、图片/PDF 分流、SSRF 拒绝、失败分类 |
| `tests/integration/test_chat_attachments.py` | 多附件落盘、消息回显、原文件回取、越权 404、拒绝不建会话 |
| `tests/unit/test_agent_loop.py` | 回路路由、收敛、同参短路、兜底作答 |
| `tests/unit/test_ocr_document.py`（MCP 部分） | 地址为空、传输失败转 `OCR_NOT_CONFIGURED`、远端 `Error:` 前缀识别、临时文件清理、入参形状兼容 |
