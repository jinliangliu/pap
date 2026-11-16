# 长期记忆

## 它解决什么

平台原来的上下文只有两种：本轮的 `messages`，以及知识库里的文档。
用户说过「我叫 alex」「周报只要结论」「我们用自研观测不用 LangSmith」，
下一轮全部丢失——每次都要重新说一遍。

记忆就是把这些**稳定事实**沉淀下来，在下一次规划前作为背景注入。
它不替代聊天记录，也不替代知识库：

| | 存什么 | 谁写 | 生命周期 |
|---|---|---|---|
| `messages` | 一轮一轮的原始对话 | 系统 | 随会话删除 |
| `memories` | 提炼后的结论 | 用户 / 抽取任务 | 用户删或过期 |
| 知识库 | 文档原文与切片 | 管理员上传 | 随文档删除 |

## 记什么，不记什么

记：称呼、身份、长期偏好、明确决策、项目背景、稳定的工作习惯。

不记：本轮参数（站点 ID、日期）、一次性的查询结果、模型对用户的猜测、
临时情绪、任何密钥与凭据。

这条线由 `memory_service` 的两条规则兜住：

1. 单条内容上限 2000 字符——写这么长说明它不是记忆，是文档，该进知识库；
2. 每轮注入有字符预算 1600（`PROMPT_BLOCK_BUDGET`），超出的部分直接省略，
   并在 prompt 里注明「其余记忆已省略」，避免模型以为自己看到了全部。

## 数据模型

`memories` 表在初始 schema 里就建好了（`54522e16c913`），本次没有加迁移。

| 字段 | 用途 |
|---|---|
| `user_id` | 非空 = 个人记忆；为空 = 团队共享记忆（所有成员可见） |
| `memory_type` | `profile` / `preference` / `decision` / `fact` / `working_note` |
| `status` | `active` / `superseded` / `deleted` |
| `source` | `user`（人写的）或 `auto_extract`（抽取的候选） |
| `is_user_confirmed` | 抽取出来的默认为 false，页面上能一键确认 |
| `confidence` | 低于 0.5 的不进 prompt |
| `expires_at` | 过了就不再注入（置为 `superseded`，不物理删） |

`status = deleted` 这个枚举保留但**不用于用户删除**：用户点删除走物理删除。
软删会让「删除」变成一次撒谎——记录还在库里，只是界面上看不见。

## 注入机制

`plan_request` 节点每轮调用 `_memory_block()`，把记忆渲染成一段文本放进
`create_action_plan` 的 payload（键名 `known_user_facts`）：

```
[已知用户信息]
- 身份｜称呼：希望被称呼为 alex，不要用「您」。
- 偏好｜回答风格：先给结论再给依据。
以上是历史对话中确认过的信息，仅作背景参考；与用户本轮明确表述冲突时，
以本轮为准，不要用这些信息反推用户意图。
```

三个细节：

- **记忆为空时 payload 里根本没有这个键**。放一个空字符串，模型会以为
  「系统给了但我没看到」，反过来去猜用户是谁。
- **只取 `confidence >= 0.5` 且未过期的**，按类型优先级排序（身份 > 偏好 >
  决策 > 事实 > 进展），再做时间倒序，最后截断到 40 条。
- **查库失败不抛异常**，只记 warning 后返回空串。记忆是让模型少问两句的
  加分项，不能变成「对话打不开」。

`MEMORY_ENABLED=false` 时跳过整段逻辑，接口仍可读写（历史还在），
排查「模型为什么突然这么答」时先关它。

## 权限

与观测页同构，但更严一档：

- 任何成员都能读写**自己的**记忆；
- 团队共享记忆（`user_id IS NULL`）只有平台管理员或持有 `memory:read` 的人能写；
- 别人的记忆**根本查不到**——不是 403 是 404。403 等于确认「这条存在」。
- 梦境建议沿用同一套边界：只处理自己的记忆。管理员可以只读查看全租户建议
  （用来判断抽取质量），但应用/忽略仍只允许本人——替别人确认等于替他改人格设定。

生产库的 `dev` 角色不会自动获得新权限（`_ensure_role` 只在角色不存在时播种），
要放开需手动执行：

```sql
INSERT INTO role_permissions (role_id, permission)
SELECT id, 'memory:read' FROM roles WHERE name = 'dev'
ON CONFLICT DO NOTHING;
```

复制粘贴时注意：`role_permissions` 是 `(role_id, permission)` 两列，
不是 `(role_id, permission_id)`。

## 自动抽取

每轮对话**答完之后**，后台用同一份文字模型判断这一轮有没有值得长期记住的事实。
抽出来的是**候选**（`source=auto_extract`、`is_user_confirmed=false`），
在记忆页标「自动 / 待确认」，确认后才当成确定信息。

为什么放在回答落地之后，而不是对话中间：

- 抽取要读完整的一问一答，中途看到的只是半截；
- 抽取失败不能影响回答，放主链路上等于给对话加了一个可失败点；
- 用户不关心这一轮有没有产生记忆，它必须是后台任务（`asyncio.create_task`，
  进程退出时丢掉一条可以接受）。

### 门槛

| 条件 | 说明 |
|---|---|
| `MEMORY_ENABLED` + `MEMORY_EXTRACT_ENABLED` | 任一为 false 就完全不产生模型调用 |
| 这一轮 `status == completed` | 等审批、超时、模型报错都没有「结论」可提炼 |
| 用户消息 ≥ 6 字符 | 「嗯」「几点」这类不可能包含稳定事实，省一次调用 |

### 流程

1. 取该用户已有记忆的标题（最多 200 条）放进 prompt，让模型自己避开重复；
2. 问一次模型，要**严格 JSON**：`{"memories":[{"type","title","content","confidence"}]}`；
3. 解析（容忍代码块围栏、顶层数组、中文类型、字符串置信度），
   丢掉空标题 / 空内容 / `confidence < 0.6`，去重后最多 3 条；
4. `memory_service.upsert_extracted` 落库，再跑一次 `enforce_cap`。

读标题与写库开两个短 session：中间隔着一次网络调用，不该占着数据库连接。

### 写库规则

- **已存在的记忆如果用户确认过、或本来就是他自己写的：只跳过，不改内容。**
  模型推断出的话不能悄悄替换用户的原话，改口由人在页面上改。
- 已存在的自动候选则刷新内容与置信度，避免同一条事实攒出多个版本。
- `working_note` 默认 30 天后过期（`WORKING_NOTE_TTL_DAYS`）：
  「正在做 M4」这种话三个月后还在 prompt 里，只会误导模型。

### 置信度就是「能不能直接用」的分界线

模型被要求：用户明确说的给 0.9，可合理推断的给 0.7，拿不准就别输出。
落库门槛 0.6 —— 低于它的连列表都不进。

注入门槛分两档：

- 用户确认过的记忆（`is_user_confirmed=true`）：`confidence >= 0.5` 就注入；
- 自动抽取的候选：要 `confidence >= 0.8`（`UNCONFIRMED_PROMPT_MIN_CONFIDENCE`）
  才注入。0.7 那一档「可合理推断」的留在记忆页等人确认——
  模型猜出来的偏好不该自己变成人格设定。

### 失败与成本

任何异常都吞掉，只留一条 `event=memory_extract_failed` 的 warning：
记忆是加分项，不能变成对话的故障点。

成本是每轮多一次小模型调用（输入约 600 token、输出约 100 token）。
觉得不划算就关 `MEMORY_EXTRACT_ENABLED`：只是不再新增候选，已有记忆照常注入。

## 梦境（dreaming）

自动抽取只看「那一轮」，三类问题它发现不了：抽取时写错（把计划写成已完成、
把助手建议写成用户决定）、用户后来改口、同一件事攒成两条标题不同的记忆。
梦境用一个离线任务把自动候选与**会话原文**对照，产出建议交给人确认。

参考 Anthropic《The components of agent memory》里的 dreaming：
不靠模型「记得更准」，而是让它事后对照 transcript 检查自己的笔记。

### 术语

命名对齐 OpenClaw 的 dreaming（`docs.openclaw.ai/concepts/dreaming`）：
一次离线运行叫**一次梦境**，它产出的待确认项叫**梦境建议**，界面入口是「开始梦境」。

概念叫梦境，底层标识沿用历史命名：表 `memory_reviews`、接口 `/memories/dream`、
环境变量 `MEMORY_DREAM_*`、权限审计 `memory.dream_config.update`。
给已上线的表改名要写迁移、还要同步所有查询，换不来任何行为改进，所以只统一了措辞。

与 OpenClaw 的差别：它有 light → REM → deep 三阶段，并把每次结果写成 `DREAMS.md`
叙事日记；这里只有一次「对照原文核对候选」。原因是候选**本来就逐条可查**
（标题、理由、证据、置信度全在列表里），再加一层叙事只是把同一批结论换个说法写第二遍。
真需要那一层，等候选多到人看不完再说——那时问题也变成「排序」而不是「叙事」。

```bash
uv run python -m scripts.dream_memories --user <user_id>
uv run python -m scripts.dream_memories --user <user_id> --dry-run
uv run python -m scripts.dream_memories --user <user_id> --days 60 --limit 30
```

页面入口在记忆页右上「开始梦境」，一次跑 5 条——按需触发，不挂在每轮对话上。
跑完的统计（扫描 / 建议 / 保留 / 失败）直接弹 toast，不另存日记文件。

四类动作：

| 动作 | 含义 | 应用后 |
|---|---|---|
| `correct` | 与用户原话不符 | 改写标题/内容，**仍为未确认候选** |
| `merge` | 与另一条说同一件事 | 目标更新，本条置 `superseded` |
| `retire` | 用户改口或已失效 | 置 `superseded`，不物理删 |
| `keep` | 证据不足或原文支持 | 不落库，不打扰人 |

**红线**：只处理 `source=auto_extract` 且 `is_user_confirmed=false` 的候选。
生成前与应用前各校验一次——建议生成后用户可能已经确认或编辑过它，
旧建议必须作废（接口返回 409），不能拿过期结论覆盖人的决定。
`correct` 应用后**不会**顺手置为已确认：模型纠错不等于用户确认。

**证据只取 user 消息**（`messages`，不是 `obs_traces`）：

- 助手自己的话不能证明用户说过什么，拿模型输出当证据等于自证；
- `obs_traces` 可以被观测开关关掉、还有保留期，靠它做长期纠错会突然「没有证据」。

证据选择：来源 thread 的消息最多 2 条（防止只看自己的出处自证），
其余按中文 bigram / 英文词与记忆标题正文的交集排序；完全无交集时补 3 条最近消息，
标记 `matched=false`，prompt 明确禁止把它们当作事实依据。

建议落在 `memory_reviews`（`action` / `reason` / `proposed_*` / `evidence_json` /
`confidence` / `model` / `prompt_version`），与 `memories` 分表：
建议有独立生命周期，同一条记忆会被反复整理，塞进 `metadata_json` 会越滚越乱。
`memory_id` 带 `ON DELETE CASCADE`，用户物理删除记忆时建议一起消失。

**指标**：`GET /api/v1/memories/extraction-stats?days=7` 返回自动候选确认率。
口径只含仍存在的候选；被物理删除的查不到，所以数字偏高，响应里的 `note` 写明了这点。
这条指标用于判断抽取 prompt 该不该改，不要当精确通过率。

降级：模型未配置 / 超时 / JSON 非法 / 编号越界 → 该条跳过（JSON 非法降级 `keep`），
不影响整批；`MEMORY_DREAM_ENABLED=false` 关闭入口。

### 配置

三个值（开关 / 回溯窗口 / 单次上限）都是**两层**的：环境变量是平台默认值，
租户可以在记忆页「梦境配置」里覆盖。存在 `tenant_memory_settings`（一行一个租户）。

| 项 | 环境变量 | 租户可覆盖 | 合并规则 |
|---|---|---|---|
| 开关 | `MEMORY_ENABLED` + `MEMORY_DREAM_ENABLED` | 三态：跟随 / 启用 / 停用 | **平台关闭是急停**，租户配 `true` 也照样关 |
| 回溯窗口 | `MEMORY_DREAM_DAYS`（默认 30） | `0` = 跟随平台 | 租户值优先 |
| 单次上限 | `MEMORY_DREAM_MAX_PER_RUN`（默认 20） | `0` = 跟随平台 | 租户值优先，超出的请求按上限跑 |

三条容易搞错的语义：

- **`0` 是「不覆盖」，不是「0 天 / 0 条」。** 落库时就存 `0`，不展开成当前默认值——
  展开等于把今天的默认值钉死在这个租户上，以后平台调默认它就不跟随了。
- **`dream_enabled` 用三态**（NULL / true / false）。布尔列分不清「没配过」和
  「明确关掉」，会把未配置的租户当成已关闭。
- **平台层关闭优先。** 否则运维想临时止血时得挨个改租户配置，急停就没有意义。

写配置需要 `memory:read` 或平台管理员，且记入审计（`memory.dream_config.update`），
因为这会改变「模型拿什么证据判断用户记忆」。读对所有成员开放——
读不到就解释不清「开始梦境」按钮为什么是灰的。

`POST /memories/dream` 的 `days` 留空表示走配置；`limit` 只表示「这一次跑几条」，
会被配置的上限夹紧（超了按上限跑，不报错——前端滑块和配置不同步是常态）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/memories` | 列表，支持 `status` / `memory_type` / `q` / 分页 |
| POST | `/api/v1/memories` | 新增，`scope=tenant` 需要权限 |
| PATCH | `/api/v1/memories/{id}` | 编辑，改完自动置为已确认 |
| DELETE | `/api/v1/memories/{id}` | 物理删除 |
| POST | `/api/v1/memories/{id}/confirm` | 确认自动抽取的候选 |
| POST | `/api/v1/memories/dream` | 手动触发一次梦境（只跑自己的，默认 10 条） |
| GET | `/api/v1/memories/dream/config` | 本租户梦境配置（已合并平台默认），所有成员可读 |
| PUT | `/api/v1/memories/dream/config` | 写梦境配置，需 `memory:read`，记审计 |
| GET | `/api/v1/memories/reviews` | 梦境建议，`status=pending` / `applied` / `dismissed` / `all` |
| POST | `/api/v1/memories/reviews/{id}/apply` | 应用建议（仅本人；状态已变返回 409） |
| POST | `/api/v1/memories/reviews/{id}/dismiss` | 忽略建议，记忆不动 |
| GET | `/api/v1/memories/extraction-stats` | 自动抽取的确认率，`days` 默认 7 |

同类型 + 同标题（归一化后）视为同一条：第二次写入是**更新**而不是新增。
不这么做的话，自动抽取跑几个月，「称呼」会攒出十几条不同说法，
注入时全部进 prompt，模型只能挑一个猜。

## 界面

侧边栏「记忆」。四张统计卡（条目 / 待确认 / 待整理 / 团队共享）+ 类型过滤 + 列表，
每行可确认、编辑、删除。删除走统一确认弹层，明确写「无法恢复」。

「梦境建议」卡片在列表上方：每条显示动作（改写 / 合并 / 下架）、原记忆标题、
理由、建议内容、证据条数与置信度，可应用或忽略。右上角「开始梦境」按需触发。
卡片底部显示近 7 天确认率，并注明口径偏差。

卡片头上显示生效参数（「近 30 天 · 单次上限 20 条」），齿轮进去是「梦境配置」弹窗：
开关（跟随平台 / 启用 / 停用）、回溯窗口、单次上限。输入框**留空表示跟随平台默认**，
placeholder 显示当前生效值——不把生效值直接填进输入框，否则一保存就变成一次
「明确设置」。梦境停用时「开始梦境」按钮直接禁用并带上原因，
而不是让人点一次再吃一个 400。

## 还没做

- **梦境没有定时任务**：目前只能点「开始梦境」或跑脚本。文章强调 dreaming 的
  cadence 要按「记忆被访问的频率」调；现在没有访问数据，先手动触发，
  等真实抽取量上来再定周期。
- **改口场景的版本留存**：`merge` 已经会写 `superseded_by_id`，
  但「用户改口 → 新版本替代旧版本」仍是直接覆盖内容，旧内容不保留。
  要保留完整改口历史得给 `memories` 增加版本行，成本高于当前收益。
- **跨用户 / 与已确认记忆的合并**：梦境只认同用户、同租户、未确认的候选。
  其余合并需要人对人的判断，不是模型能拍板的。
