# 从文件新建（统一导入）

控制台侧栏的「从文件新建」：拖一份文件进去，按**内容**判断它该建成知识库文档、
技能还是工具，预览确认后再写库。

**构建助手的加号不通往这里**：那边选中的文件是这个会话的参考材料，随消息走附件通道
（模型用 `ocr_document` 读），会话删除时才回收。建知识库只走这个页面——它要的是
原文切片，还要预览判类、权限与内容幂等，这些都不该在对话里顺手做掉。

为什么不是「知识库页挂一个上传钮、技能页挂一个、工具页再挂一个」：
判断依据是文件内容而不是后缀——现场规程和技能都是 `.md`，工具导出和 OpenAPI
都是 `.json`。让人先自己分类，等于把机器能干的事推给人，判错还要改名重传。

```
侧栏「从文件新建」
      │  POST /api/v1/studio/import   (multipart: file / target / options / dry_run)
      ▼
app/studio/importer.py ── 判类 + 权限 + 组织预览
      │
      ├── knowledge → app/knowledge/upload.ingest_upload      （与页面上传、MCP 上传同一份）
      ├── skill     → skill_service.upsert_imported_skills    （与技能页导入同一份）
      └── tool      → custom_tool_service.import_tools        （与工具页导入同一份）
```

**三种写入都不在这里重写。** 只要有一处导入规则在这个模块里再抄一遍，就会长出
「页面上传能过、从文件新建不能过」这类只在一条路径上复现的差别——而那时最难判断。

## 1. 支持的文件与识别规则

| 文件 | 识别结果 | 判据 |
|---|---|---|
| `.md` / `.markdown` 且开头有 YAML front matter（含 `name`/`display_name`/`trigger_keywords`/`allowed_tools` 任一） | 技能 | 知识库文档不会在开头写 YAML |
| 其余 `.md` / `.txt` / `.pdf` / `.docx` / `.html` | 知识库文档 | 后缀在白名单内 |
| `.json` / `.yaml` 且顶层有 `openapi: 3.x` 或 `swagger: 2.x` | 工具（每个 operation 一个） | 版本字段 |
| `.json` / `.yaml` 且顶层是 `{"tools": [...]}` 或 `[{method,url}, …]` | 工具（导出文件） | 平台自己的导出形状 |
| `.json` / `.yaml` 且顶层是 `{"skills": [...]}` 或 `[{instruction}, …]` | 技能（导出文件） | 同上 |
| 其余 | **400，不猜** | 判不出来时说清楚收什么，而不是塞进知识库 |

`.json/.yaml` **不进知识库**：它们是配置，不是资料，切成片只会污染检索。

判错了就地改：每行的「导入到」下拉可以覆盖识别结果，改完立刻重新预览。
`detected_target` 与 `target` 分开回传，所以界面能同时显示「它自己认成了什么」
和「你让它去哪里」。

## 2. 接口

### `GET /api/v1/studio/import/targets`

前端据此渲染下拉与提示，权限矩阵只在这一处维护。返回 `targets[]`、`max_file_bytes`、
`is_platform_admin`。

### `POST /api/v1/studio/import`

`multipart/form-data`：

| 字段 | 说明 |
|---|---|
| `file` | 必填，单份 |
| `target` | `auto`（默认）/ `knowledge` / `skill` / `tool` |
| `dry_run` | `true` 只预览不写库 |
| `options` | JSON 字符串（multipart 没有嵌套对象） |

`options` 按目标不同：

```jsonc
// knowledge
{"title": "调压箱操作规程", "access_scope": "internal"}
// skill
{"name": "valve-patrol-2", "overwrite": true}
// tool
{"base_url": "https://real.example.com", "operations": ["getstation"], "mode": "merge"}
```

响应（预览与写入同形，写入时多一个 `result`）：

```jsonc
{
  "filename": "valve.md", "size_bytes": 214,
  "target": "skill", "detected_target": "skill", "dry_run": true,
  "notes": ["..."],
  "preview": { "skills": [ { "name": "...", "exists": false, "instruction_head": "..." } ] }
}
```

`result` 的形状按目标不同：知识库是 `{action, document_id, chunk_count, access_scope, message}`
（`action` ∈ `created`/`updated`/`unchanged`），技能与工具是 `{action, created, updated, skipped, items[], message}`。

**`dry_run` 与真正写入调用同一个解析函数。** 前端另算一遍预览，迟早出现
「预览显示 3 个接口、实际存进去 1 个」，而那是导入之后才发现的错。

## 3. 权限

照各页面原有口径，不新造一套：

| 目标 | 要求 | 违反时 |
|---|---|---|
| 知识库 | `knowledge:write`（高分级还要对应 scope） | 403 |
| 技能 | `skill:write` 或平台管理员 | 403 |
| 工具 | 平台管理员 | 403 |

403 与 400 分得很清：**403 是去申请权限，400 是换文件或改参数。** 写成一句
「导入失败」，人会先怀疑文件。

权限在 `dry_run` 时也照样判（与知识库页面上传一致）：没有写权限的人拖进来的第一眼
就该知道「这份东西你建不了」，而不是先看一屏预览、点导入才被拒。

写操作照旧落 `ADMIN_ACTION` 审计（只记文件名、大小、目标、数量，不记正文）。

## 4. 几条刻意的取舍

- **不静默丢弃。** OpenAPI 里平台支持不了的东西（数组参数、cookie、`Authorization`
  这类凭据头、非法标识符参数名、超过 32 个参数）一律进 `notes` 显示出来。
  少一个参数要到调用时才暴露，那时人已经不知道是导入时丢的了。
- **相对服务地址直接拒绝。** 规格里 `servers[0].url` 写成 `/v1` 时，拼出来的工具
  URL 存下去也调不通，与其让人调用时才发现，不如在导入时说清楚、并留 `base_url` 覆盖。
- **技能名可以改。** 同一份模板要建第二个技能时改 `options.name`，而不是复制文件。
- **工具默认全选、可逐条去掉。** 几十个 operation 逐个勾是负担，但一导入就全注册
  也不对。
- **同名技能默认覆盖**（与技能页导入一致），预览里会明说「已存在，导入会覆盖」。
- **重复拖同一份文件**（同名同大小）在列表里自动去重。

## 5. 没做的

- **技能包（目录 / zip）**：带脚本、模板的技能包要定目录规范、存附件、防路径穿越，
  不在这一版。现在是「一个 `.md` 一个技能」。
- **`curl` 命令导入**：工具页已有 `/api/v1/admin/tools/parse-curl`，从浏览器
  DevTools 复制的那条路径没变，这里不重复实现第二份。
- **版本 diff 预览**：覆盖同名技能时不显示「哪几行变了」。指令全文在预览里，
  够判断，但不够对照。
- **一次多文件的事务性**：每个文件各自成败，一个失败不影响其余（列表里逐行给原因）。

## 6. 排障

| 现象 | 先看这里 |
|---|---|
| 「不认识这份 JSON」400 | 顶层既没有 `openapi`/`swagger`，也没有 `tools`/`skills` 数组。工具导出与 OpenAPI 是两种形状 |
| 技能被建成了知识库文档 | front matter 里没有 `name`/`description` 这类字段，或开头不是独占一行的 `---`（BOM 可以，空行不行） |
| 工具 URL 少了协议与域名 | 规格里 `servers[0].url` 是相对地址；填 `base_url` 或改规格 |
| 导入成功但工具列表里没有 | 自定义工具是平台级资源，看的是 `#/tools` 的平台管理员视图；非管理员看不到 |
| 知识库「已导入但没有切片」 | 不可能同时发生：`action=created` 一定带 `chunk_count≥1`；`chunk_count=0` 只出现在 `unchanged` |
| 传上去 413 | 超过 `KNOWLEDGE_MAX_FILE_BYTES`（与页面上传同一个上限，按上限流式读取，不会先把进程撑爆） |
