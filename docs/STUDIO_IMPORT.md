# 从文件导入（附件 → 知识库 / 技能 / 工具）

在构建助手里点加号选一份文件，再说一句用途——「把这份规程入库」「按这份 OpenAPI
加工具」「这份 md 做成技能」——它按**内容**判类并写库，全程不用离开对话框。

**不再有单独的「从文件新建」入口。** 判类的依据是文件内容 + 用户这句话说的用途，
而两者都在对话里；先让人去侧栏选一个目标，等于把机器能干的事推给人（现场规程和
技能都是 `.md`，工具导出和 OpenAPI 都是 `.json`），判错还要改名重传。

```
构建助手抽屉：加号选文件 + 一句话说明用途
      │  附件落盘成句柄（txt_xxx / doc_xxx），随会话保留
      ▼
studio_import 工具（app/tools/builtins/studio_admin_tools.py）
      │  按 file_id 取磁盘原文，原始文件名从消息元数据里找回
      ▼
app/studio/importer.py ── 判类 + 权限 + 组织结果
      │
      ├── knowledge → app/knowledge/upload.ingest_upload      （与页面上传、MCP 上传同一份）
      ├── skill     → skill_service.upsert_imported_skills    （与技能页导入同一份）
      └── tool      → custom_tool_service.import_tools        （与工具页导入同一份）
```

**三种写入都不在这里重写。** 只要有一处导入规则在这条链路上再抄一遍，就会长出
「页面上传能过、对话里导入不能过」这类只在一条路径上复现的差别——而那时最难判断。

`POST /api/v1/studio/import`（multipart）仍在，是同一套 `run_import` 的 HTTP 形态，
留给脚本与将来的批量预览；控制台自己不再调用它。

## 1. 为什么不先把正文读出来再写

读取工具（`ocr_document`）单次约 4 万字符，长文档会被截断（保留开头与结尾并注明）。
拿截断后的正文去 `studio_knowledge_add`，得到的是一份**看着完整、实际少了一半**的
资料——失败时没有任何报错，检索出来才发现少章节。

`studio_import` 读的是磁盘上的原文，按整份切片，与页面上传同一条路径。
`studio_knowledge_add` 因此只剩一个用途：写用户口述、或直接贴进对话的一段正文。

## 2. 支持的文件与识别规则

| 文件 | 识别结果 | 判据 |
|---|---|---|
| `.md` / `.markdown` 且开头有 YAML front matter（含 `name`/`display_name`/`trigger_keywords`/`allowed_tools` 任一） | 技能 | 知识库文档不会在开头写 YAML |
| 其余 `.md` / `.txt` / `.pdf` / `.docx` / `.html` | 知识库文档 | 后缀在白名单内 |
| `.json` / `.yaml` 且顶层有 `openapi: 3.x` 或 `swagger: 2.x` | 工具（每个 operation 一个） | 版本字段 |
| `.json` / `.yaml` 且顶层是 `{"tools": [...]}` 或 `[{method,url}, …]` | 工具（导出文件） | 平台自己的导出形状 |
| `.json` / `.yaml` 且顶层是 `{"skills": [...]}` 或 `[{instruction}, …]` | 技能（导出文件） | 同上 |
| 其余 | **拒绝，不猜** | 说清楚收什么，而不是塞进知识库 |

`.json/.yaml` **不进知识库**：它们是配置，不是资料，切成片只会污染检索。

**判类要用原始文件名。** 附件在磁盘上只按句柄存（`txt_xxx.txt` / `doc_xxx.pdf`），
原始名字留在 `messages.metadata_json.attachments[].name`；工具侧传了 `filename`
就用它，没传就回消息里找。两处都没有时按 MIME 兜底成 `.txt`，并在结果里明说
「判类可能不准，请显式指定 target」——不声不响地按错误类型入库，比拒绝难查得多。

用户说了用途就以用户为准：`target` 显式传 `knowledge`/`skill`/`tool` 时跳过自动判类，
结果里同时回传 `detected_target`，所以能说清「它本来判成技能，我按知识库存了」。

附件只收三类：图片、PDF、文本（`.md/.txt/.log/.json/.yaml/…`，见
`attachment_store._TEXT_SUFFIXES`）。图片**不能导入**——没有「原文切片」这回事，
要看图里的文字用 `ocr_document`。

## 2.1 入库前的形态清洗（OCR 的 HTML 表）

PaddleOCR-VL 有时直接吐 `.md`，里面的表格仍是
`<table border=1 style='word-wrap: break-word;'><tr><td rowspan="2">`。
这份东西如果原样入库，后果不是「不好看」，是「不能要」：

| 指标 | GB/T 3836.4—2021 的 OCR 产物 |
|---|---|
| 原始字符 | 568,397 |
| 其中 HTML 标签占比 | **77%** |
| 原样切片 | 973 片（超上限被拒），平均每片 584 字，最小 2 字 |
| 清洗后 | 140,764 字、287 片、标签 0 |

所以 `extract_text_from_bytes` 在解析文本类文件后会调用
`app/knowledge/markdown_clean.py`：表格转 pipe 表、**合并单元格摊平成等宽网格**、
去掉 `div/span` 包裹层（题注文字留下）、压掉多余空行。

**这不是格式化，也不是替用户改内容。** 表格信息一点没丢：`<td>1 000</td>`
进索引是 `1`/`000` 两个噪声词，`| 1 000 |` 才是一个数。合并单元格必须摊平，
否则表头 3 格、数据 8 格时后面的列整体前移——引用里看起来像「适用范围写反了」，
那是事实被读错，不是排版丑。

清洗放在 `extract_text_from_bytes` 而不是导入工具里，因为它有五个调用点
（页面上传、目录扫描、统一导入、附件读取、预览）：只在一处清洗，其余四处的
文档就会带着标签入库，而它们看起来「都正常」。

清洗结果会回传（`result.cleanup` / `preview.cleanup`），消息里明说
「原文 568,397 字，清洗后 140,764 字，差额是标签与样式属性，正文没有删减」——
不说这句，人会以为后半本没进去，或者以为自己传错了文件。

没有标签味的 md 原样返回，不白跑一遍，也不动它的空行。

**单篇切片上限是可调的**（控制台「运行配置 → 入库容量」，即时生效）。
它是容量阈值而不是内容判据：一份 56.8 万字的现行国标该有 973 片，一本误传的
几 MB 手册才该被拦。所以撞上限时的报错会给出真实片数、原文长度与平均每片字数，
让人自己判断是「大部头」还是「切碎了」——而不是只说一句「请拆分」，
那是句做不到的指令（导入入口没有拆分功能，手工拆还会把同一份标准散成两处）。

## 3. 工具参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `file_id` | 是 | 消息 `[附件]` 里的句柄，原样传 |
| `target` | 否 | `auto`（默认）/ `knowledge` / `skill` / `tool` |
| `filename` | 否 | 清单里的「文件名=」原值；判类依赖后缀 |
| `title` | 否 | 知识库标题，默认用文件名去后缀 |
| `access_scope` | 否 | 知识库分级，默认 `internal` |
| `name` | 否 | 技能标识，默认取 front matter 里的 `name` |
| `mode` | 否 | 导入工具时同名处理，`merge`（默认）/ `overwrite` |

工具是 `WRITE` + 需审批一次。**没有 `dry_run` 参数**：判类预览要单独一次调用，
而写工具的风险级别是静态的——「预览也要审批」会让人对着一张什么都没改的卡点确认。
需要预览的场合走 HTTP 接口的 `dry_run`。

`title` / `access_scope` 由助手自己定（拿不准用 `internal`），不拿这两项去问用户。

## 4. 权限

照各页面原有口径，不新造一套：

| 目标 | 要求 | 违反时 |
|---|---|---|
| 知识库 | `knowledge:write`（高分级还要对应 scope） | 拒绝 |
| 技能 | `skill:write` 或平台管理员 | 拒绝 |
| 工具 | 平台管理员 | 拒绝 |

工具的 `required_scopes` 是空的——三类目标的权限本来就不同，写死在定义里会让
「只导入知识库的人」连工具都看不到。真正的判定在 preflight（按 `target` 粗判，
让人别对着注定失败的审批卡点确认）与 `run_import`（按实际落点细判）两处。

写操作落 `ADMIN_ACTION` 审计，`resource_type` 按目标取
`knowledge_document` / `skill` / `custom_tool`，metadata 记 `file_id`、文件名、
`target`、`detected_target` 与结果数量，**不记正文**。

## 5. HTTP 接口（仍在，供脚本与预览）

`GET /api/v1/studio/import/targets` 返回 `targets[]`、`max_file_bytes`、`is_platform_admin`，
权限矩阵只在这一处维护。

`POST /api/v1/studio/import`，`multipart/form-data`：

| 字段 | 说明 |
|---|---|
| `file` | 必填，单份 |
| `target` | `auto`（默认）/ `knowledge` / `skill` / `tool` |
| `dry_run` | `true` 只预览不写库 |
| `options` | JSON 字符串（multipart 没有嵌套对象） |

```jsonc
// knowledge
{"title": "调压箱操作规程", "access_scope": "internal"}
// skill
{"name": "valve-patrol-2", "overwrite": true}
// tool
{"base_url": "https://real.example.com", "operations": ["getstation"], "mode": "merge"}
```

`result` 的形状按目标不同：知识库是
`{action, document_id, chunk_count, access_scope, cleanup, pipeline, tags, message}`
（`action` ∈ `created`/`updated`/`unchanged`），技能与工具是
`{action, created, updated, skipped, items[], message}`。

知识库多出来的三个字段是**同一份入库快照的两个视角**：`cleanup` 是清洗统计，
`pipeline` 是完整八阶段快照（与 `documents.metadata_json.pipeline` 同一份，
治理页读的也是它），`tags` 是打标结果。对话里的工具回复由它们拼成一条
「加工链路」——`解析 → 清洗 → 结构化 → 切片 → 打标 → 索引`，外加一行标签。
没有快照时（老行、技能/工具）退回只说片数，不拿 `0 张表` 冒充事实。

导入落在知识库时，工具会把 `document_id` 放进 `external_operation_id`，
审批确认后的回读核验按它**另起一个会话**再查一遍：文档在、状态 `ready`、
切片行数大于 0，三条都成立才把审批推进到 `VERIFIED`。技能与工具导入不落
`documents` 表，没有可回读的落点，状态停在 `EXECUTED` 并写明原因——
不拿「接口返回成功」冒充核验。

**`dry_run` 与真正写入调用同一个解析函数。** 另算一遍预览，迟早出现
「预览显示 3 个接口、实际存进去 1 个」，而那是导入之后才发现的错。

## 6. 几条刻意的取舍

- **不静默丢弃。** OpenAPI 里平台支持不了的东西（数组参数、cookie、`Authorization`
  这类凭据头、非法标识符参数名、超过 32 个参数）一律进 `notes` 显示出来。
  少一个参数要到调用时才暴露，那时人已经不知道是导入时丢的了。
- **相对服务地址直接拒绝。** 规格里 `servers[0].url` 写成 `/v1` 时，拼出来的工具
  URL 存下去也调不通，与其让人调用时才发现，不如在导入时说清楚、并留 `base_url` 覆盖。
- **技能名可以改。** 同一份模板要建第二个技能时传 `name`，而不是复制文件。
- **同名技能默认覆盖**（与技能页导入一致），结果里说明覆盖了几个。
- **导入结果必须说清三件事**：识别成什么、实际写成什么、写了多少（切片数 / 新建与
  覆盖条数）。只回一句「导入成功」，判错类型时没人会发现。

## 7. 没做的

- **技能包（目录 / zip）**：带脚本、模板的技能包要定目录规范、存附件、防路径穿越，
  不在这一版。现在是「一个 `.md` 一个技能」。
- **`curl` 命令导入**：工具页已有 `/api/v1/admin/tools/parse-curl`，不重复实现第二份。
- **版本 diff 预览**：覆盖同名技能时不显示「哪几行变了」。
- **一次多文件的事务性**：一份文件一次调用；多个文件的成败各自独立。
- **图片入库**：只能靠 OCR 出一段文本，与页面上传（直接解析 PDF/Word/Markdown）
  不是同一条路，宁可让人先确认。

## 8. 排障

| 现象 | 先看这里 |
|---|---|
| 「不认识这份 JSON」 | 顶层既没有 `openapi`/`swagger`，也没有 `tools`/`skills` 数组。工具导出与 OpenAPI 是两种形状 |
| 技能被建成了知识库文档 | front matter 里没有 `name`/`description` 这类字段，或开头不是独占一行的 `---`（BOM 可以，空行不行） |
| 结果里说「没有留下原始文件名」 | 客户端上传时没给 `name`。显式传 `target`，或让用户重传一次 |
| 工具 URL 少了协议与域名 | 规格里 `servers[0].url` 是相对地址；填 `base_url` 或改规格 |
| 导入成功但工具列表里没有 | 自定义工具是平台级资源，看的是 `#/tools` 的平台管理员视图；非管理员看不到 |
| 知识库「已导入但没有切片」 | 不可能同时发生：`action=created` 一定带 `chunk_count≥1`；`chunk_count=0` 只出现在 `unchanged` |
| 附件传不上去 | 文本上限 4 MB（`_TEXT_MAX_BYTES`），PDF 走 `OCR_MAX_PDF_BYTES`；前端与后端两个上限必须同步 |
