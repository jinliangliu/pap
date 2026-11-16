# 会话历史

对话台左侧的列表来自 `conversations` / `messages` 两张表，不是浏览器
本地记录，也不是 checkpoint 的投影。刷新页面、换设备都能看到同一份历史。

## 1. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/conversations?limit=50&offset=0&channel=studio` | 当前身份的会话列表，按最近活跃倒序 |
| GET | `/api/v1/conversations/{id}` | 会话详情 + 全部消息（时间正序） |
| PATCH | `/api/v1/conversations/{id}` | 重命名（只改标题） |
| DELETE | `/api/v1/conversations/{id}` | 销毁会话：记录、运行摘要、图状态、附件文件 |

响应里的 `thread_id` 是**客户端可见**的线程标识（`client_thread_id()` 还原后的值），
内部 ID 形如 `tenant:user:client`，不对外暴露。切换历史会话后继续发消息时，
前端必须把 `state.chat.thread` 设为这个值，才会复用同一段上下文。

### 1b. `channel`：会话是哪个入口开的

`conversations.channel` 只有两个值，由**服务端按应答的 Agent 判定**，客户端报不了：

| 值 | 含义 |
|---|---|
| `api` | 其余全部（对话台、脚本、集成） |
| `studio` | 构建助手面板开的会话（`docs/STUDIO_ASSISTANT.md`） |

三条约定：

- **只在创建时写一次。** 它是「这条会话从哪儿开始的」，不是「最近一条消息从哪发的」。
  每条请求都改的话，同一条线程被两个入口用着时会来回跳标签，历史看起来像丢过一段。
- **`?channel=` 只收窄，不放宽。** 它叠在 (租户, 用户) 之上，不带时与原来逐字一致；
  带 `studio` 时看不见别人的，也看不见别的入口的。
- **列表与计数共用一份条件**（`_owned_conditions`）。各写一遍必然走散，
  现象是「列表 3 条、总数 20」，看着像分页坏了。

构建助手面板打开时就是靠这条接口把自己的历史读回来的：先按 `channel=studio` 取列表，
按浏览器里存的 thread 找到那一条，再取详情。

**对话台反过来显式带 `channel=api`。** 不带参数时两侧共用一份列表，左侧栏会混进
「新建智能体…」的确认记录，点开又是一串与业务对话无关的审批卡片。两边各带一个
channel 之后，助手线程只在面板里出现——**所以助手的操作必须另外记进审计**
（`docs/STUDIO_ASSISTANT.md` 第 10 节），否则那些变更在界面上就再也找不到来源了。

`channel` 是后加的列，加之前开的会话一律是 `api`。要回填就按**证据**判，
不要按线程名前缀猜：`obs_traces.agent_name` 记了那一轮是谁应答的。

```sql
update conversations c set channel = 'studio'
where c.channel <> 'studio'
  and exists (select 1 from obs_traces t
              where t.thread_id = c.thread_id and t.agent_name = 'studio');
```

## 2. 隔离

三个条件同时成立才算「自己的会话」：

```python
Conversation.id == conversation_id
Conversation.tenant_id == identity.tenant_id
Conversation.user_id == identity.user_id
```

路径参数只用来定位记录，不参与身份判断；任何一条不满足一律 404，
不区分「不存在」与「不是你的」——后者会泄露别人有哪些会话 id。

## 3. 删除语义：销毁，不是隐藏

一次对话会落在**四处**，只删其中一处等于没删：

| 位置 | 内容 | 清理方式 |
|---|---|---|
| `conversations` / `messages` | 用户原话、回答、附件句柄 | 同一事务内显式 `DELETE` |
| `agent_runs` | `input_summary` / `output_summary` 存了原话与回答的前 2000 字 | 同一事务内按 `conversation_id` 删行 |
| LangGraph checkpoint（三张表） | 完整 `messages`、`attachments`、`tool_observations`（含 OCR 正文） | 事务提交后 `adelete_thread(thread_id)` |
| 磁盘 | 附件原文件、会话产物 | 事务提交后删目录与对象存储里的键 |

为什么必须显式删 `agent_runs`：它的外键是 `ON DELETE SET NULL`。删会话后记录仍在，
只是失去归属——列表看着干净了，摘要还躺在库里。

为什么图状态必须在事务**之后**删：checkpoint 与文件删掉就回不来，而业务事务仍可能失败。
反过来最多短时间多占一点磁盘。

删除时如果在 `conversations` 里还能找到同一 `thread_id` 的别的会话，则跳过图状态清理。
当前 `thread_id` 有唯一约束，这条恒不触发，但删除不可逆，留一层保险。

历史残留（V1.4.0 之前只删了前两处）用 `scripts/purge_orphan_traces.py` 处理：
默认只报告，判定前提是线程在 `conversations` 里完全不存在。

## 3b. 附件：落盘、回显与清理

`POST /api/v1/chat` 接受 `attachments[]`（`{data_url, name}`），支持三类：

| 类型 | 扩展名 | MIME | 怎么读 |
|---|---|---|---|
| 图片 | `.jpg/.png/.webp/.bmp/.gif` | `image/*` | 文件头嗅探，走 OCR |
| PDF | `.pdf` | `application/pdf` | 文件头嗅探（`%PDF-`），走 OCR |
| 文本 | 见下 | `text/markdown` / `text/plain` | 后缀判定，**直接读原文** |

文本类收的是一组后缀，而不是「Markdown 与 .txt」两条：

| 用途 | 后缀 |
|---|---|
| 文档 | `.md` `.markdown` `.txt` `.text` `.rst` |
| 运行日志 / 追踪导出 | `.log` `.jsonl` `.ndjson` |
| 数据与配置 | `.json` `.yaml` `.yml` `.toml` `.ini` `.cfg` `.conf` `.env` `.csv` `.tsv` `.xml` |

按用途挑后缀等于让用户先猜「我的文件算哪一类」，而这三类都是纯文本，
读取链路完全一样。客户端声明 `application/json` / `text/yaml` / `text/csv`
这类 MIME 也收，统一归一化成上面两个规范类型——`kind_of()` 只认这两个，
留着 `application/json` 会把附件判成图片、进而送错引擎。

一条消息最多 6 个、原始字节合计 40 MB；文本另限 4 MB（真正的闸在读取时的 4 万字符）。
旧字段 `images[]` 仍兼容，会被合并进同一份清单。

**长文本保留开头与结尾**：图片 / PDF 只截前 4 万字符（文档按页顺序读），
文本类则保留前 70% 与后 30%——日志与追踪记录的关键一行几乎总在最后，
只留开头会出现「日志读了两千行，结论是启动正常」，而它启动之后就崩了。
截断处写明总字符数与省略量，模型能如实告诉用户「中间没读到」。

**文本为什么不能只靠 MIME**：浏览器对 `.md` 的 `File.type` 常常是空串，
data URL 头会变成 `application/octet-stream`。所以服务端在文件头嗅探之外，
只对后缀在白名单内的文件做一次文本判定：无 NUL、控制字符占比 < 1%、
能按 UTF-8 或 GB18030 解码。三条全过才收下，并**统一转成 UTF-8 字节落盘**——
否则读取时按 UTF-8 打开会得到一片乱码，而乱码看起来很像「原文就是这样」。
后缀不认识、声明也不是文本类型的文件不猜（`.docx` 改名成 `.md` 会被拒）。

链路：

1. **先校验、后扣配额、再建会话**。格式不对 / 太大 / 太多的请求直接返回
   `status="rejected"`、`error_category="invalid_attachment"`，
   不消耗额度，也不留下一条只有问题没有回答的空会话；
2. base64 解码后**按文件头**判断真实类型（声明类型与内容不符以内容为准），
   文本类按上面的规则判定；落盘为
   `{OCR_ATTACHMENT_DIR}/{tenant}/{conversation}/{attachment_id}.{ext}`，
   句柄前缀区分类型：`img_` / `doc_` / `txt_`；
3. 数据库只存句柄：`messages.metadata_json.attachments = [{attachment_id, kind, mime, size, name}]`。
   原文件不进库、不进 checkpoint；
4. `GET /api/v1/conversations/{id}` 会把这些句柄还原成带 `url` 的清单，
   前端据此显示缩略图 / PDF 预览 / 文本文件卡；`url` 指向
   `GET /api/v1/conversations/{id}/attachments/{attachment_id}`。

原文件接口的要点：

- 鉴权与列表、详情一致：先按 id + tenant + user 确认会话归属，再按 (租户, 会话) 定位文件。
  `attachment_id` **不是凭据**，别人的句柄拿到也是 404；
- 用 `Content-Disposition: inline` 而不是 `attachment`，否则浏览器直接下载，
  气泡里的预览会空白；
- 响应头 `Cache-Control: private, max-age=86400`：句柄对应内容不变，可以缓存，但只允许私有缓存。

清理：删除会话时一并删掉该目录（`purge_conversation()`，用 `asyncio.to_thread` 包住，
`rmtree` 同步执行会卡住事件循环）。放在 DB 提交**之后**——
文件删了但事务回滚会留下「记录在、文件没了」的坏状态，反过来只是短时间多占磁盘。

**构建助手的附件走同一套规则**（2026-11-17 起）。它由内置助手应答（`channel=studio`），
附件一样落盘、一样写进 `messages.metadata_json.attachments`、一样在会话删除时回收。
早先它被当成「这一轮的材料」、答完即删，结果是反问之后用户回一句就得到
「附件不存在或已过期」——会话才是用户组织材料的单位，按调用栈管文件会让他
记住平台什么时候清空他的东西，而这件事没人能记住。

每轮上下文里带两段清单：**本条消息新增**与**本会话此前上传（仍可读）**。
只列新增，模型会以为这轮没材料而请用户重传；只列全部，它会拿最早上传的那份
答新问题。读取工具按句柄解析，句柄仍然要过 (租户, 会话) 目录，
所以清单里多列一份也接不到别人的文件。

## 4. 为什么消息要显式写 `created_at`

`messages.created_at` 的建表默认值是秒级的 `CURRENT_TIMESTAMP`，
而一轮问答里的 user / assistant 两条消息通常在同一秒内写完。
如果按默认值排序，顺序会退化成 UUID 的随机顺序，历史回放会出现
「回答在前、问题在后」。

所以 `add_message()` 显式写入 `datetime.now(UTC)`（微秒精度）。
排序仍是 `created_at ASC, id ASC`，`id` 只是同微秒的兜底。

## 5. 时间字段的时区

PostgreSQL 的 `timestamptz` 读出来带时区；SQLite 的 `DateTime(timezone=True)`
只是约定，读出来是 naive。响应模型统一用 `as_utc()` 补 `UTC`，
否则前端会按本地时区解释，出现「刚创建显示 8 小时前」。

## 5b. 请求超时与中断

单次对话有总时限 `CHAT_TIMEOUT_SECONDS`（默认 160 秒，由 `asyncio.timeout`
包住 `graph.ainvoke`）。超时不是可选项：

- **客户端断开不会取消服务端协程**。Nginx 在 `proxy_read_timeout`（当前 240 秒）
  到了就返回 504，但后端仍在跑——2026-11-04 曾因此空转 45 分钟、521 次模型调用。
- 总时限必须**明显大于** `OCR_PDF_TIMEOUT_SECONDS`（120 秒），
  否则大 PDF 永远死在总闸上，识别工具自己的超时形同虚设。
- 超时后服务端自己收尾：`finish_run(FAILED, error_category="chat_timeout")`，
  并往会话里补一条 assistant 消息「这次请求超过 N 秒仍未完成，已中止」。
  用户刷新会话能看到结论，而不是只看到自己的提问。
- 进程重启会把遗留的 `RUNNING` 运行标记为 `FAILED / process_restarted`
  （`interrupt_stale_runs()`，逐租户执行——`agent_runs` 挂了 RLS，
  没有租户上下文的 UPDATE 影响 0 行且不报错）。

已知残留：超时被取消的图可能在下一次同 thread 请求里从 checkpoint 恢复，
`tool_steps` 会沿用旧值。这是刻意的取舍——宁可少给一轮工具预算，也不清空现场。

## 6. 已知限制

- 没有全文搜索、没有按时间范围筛选；
- 没有导出会话的功能；
- 删除会话不会回头清 `audit_logs`：审计只追加，且按设计不含对话原文；
- 列表的 `title` 取自第一条用户消息的前 60 字，之后不会自动更新（可以手动重命名）。

### 6b. 消息里都存了什么

一条 assistant 消息的 `metadata` 是回放时的唯一线索，目前有：

| 字段 | 用于 |
|---|---|
| `intent` | 区分正常回答 / 报错 |
| `error_category` | 超时、工具被拒等失败原因 |
| `tool_name` | 这一轮调了哪个工具 |
| `approval_id` | 这张审批卡对应的审批单 |
| `approval_action` | `confirm` / `cancel`，标记这条是审批的结论 |
| `ocr_text` | 识别原文，供展开核对 |

**等审批的那一句也落库**（带 `approval_id`），审批的结论也落库（带 `approval_action`）。
这两条不写的话，刷新后会话里只剩一句没有下文的提问——用户会以为操作丢了，
再发一遍，于是多出一张重复的审批单。

回放时审批卡的**状态按现在算**：`messages` 里只有审批号，参数与状态都在
`approval_requests` 上，前端按 id 现查一次（`GET /api/v1/approvals/{id}`）。
所以刷新后：
- 还挂着 → 卡片带着「确认执行 / 取消」两个按钮，可以直接接着点；
- 已确认、已取消、已过期 → 卡片显示「已处理」和具体结果，按钮不再出现。
  过期判定不只信状态：状态可能还写着 `PENDING`，但过期时间已过（清理任务没跑到），
  这时继续给按钮只会让人点出一个 409。
