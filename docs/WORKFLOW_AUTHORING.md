# 用一段话建一张图：编排的起草层

> 相关：[`ORCHESTRATION.md`](ORCHESTRATION.md)（图怎么跑）、
> [`STUDIO_ASSISTANT.md`](STUDIO_ASSISTANT.md)（谁在建）、
> `app/orchestration/definition.py`（什么算「能跑」）。

## 0. 一句话

**把「起草」与「保存」拆开。** 现在这两件事被绑在同一个工具里，所以模型每想试一下，
都要付一次审批卡的代价——它只能赌一把写完整张图；赌错了，整份重写。

## 1. 现状：能力有了，路径不顺

「用自然语言建流程」在 PAP 里不是新功能，构建助手已经能做（`studio_workflow_save`），
提示词里也写清了「用户描述有先后顺序、多角色接力、按条件分流的过程 = 要建流程」。
不顺的是路径：

| 环节 | 现在 | 卡在哪 |
|---|---|---|
| 发现 | 只在右下角构建助手面板里 | 对话页的下拉**有意**不列构建助手（`console.js` `loadChatTargets`），说「帮我建个流程」得到的是解释 |
| 起草 | 直接调 `studio_workflow_save` | 起草即保存：一进审批卡、二进库、三涨 `revision`。改一个字要重走一遍 |
| 校验 | 保存时才编译，错误一次性抛给模型 | 模型拿到「缺兜底边」的反应通常是**重发整份 JSON**，而漏掉的那个节点等于被删掉 |
| 改一处 | 只能整份重存 | 「把第二步换成 ops」在数据层是一次全量替换，提示词里得写「不要凭印象重写，漏掉的节点等于被删掉」——**能用工具形状解决的问题，不该靠提示词兜** |
| 回看 | 保存后才有 `definition` | 中途没有任何可看的东西，用户对着一句「已建好」的承诺点确认 |

对照扣子那套「AI 生成式工作流」，差距不在模型能力，在**这几个环节的次序**：它先在对话里
反复改、在画布上反复看，最后才「发送并执行变更」。我们把它压缩成了一次提交。

## 2. 扣子那套，抄什么、不抄什么

| 扣子 | PAP 对应物 | 结论 |
|---|---|---|
| 自然语言 → 生成**工作流代码**（语义层 / 参数层 / 代码层） | 自然语言 → 生成**受约束的图**（`definition` + `compile_definition`） | **不抄**。见 §3 |
| 画布预览，点节点看输入输出 | `_workflow_graph()` + 控制台编排页的结构化编辑器 | 已有骨架。画布缺独立坐标列，另案 |
| 框选节点 → **引用** → 只改这一处 | **没有** | **抄**，落成 `patch`（§5） |
| 试运行 + 每节点耗时/输入输出 | `workflow_runs.nodes_json` + 黑板 | 已有。但不做「假跑」，见 §7 |
| 测试失败 → 一键修复 | `DefinitionError.node` + `_preflight_workflow` | **半抄**：给结构化问题清单，不给自动改图 |
| 对话里反复改，工作副本随身带 | **没有**（每次都是全量提交） | **抄**，落成 `studio_workflow_draft`（§4） |
| 回滚版本 | 只有 `revision` 号，没有快照 | 用 `write_backups`（§6） |
| 上传附件 / 技能 / 选模型 | `studio_import` 已通 | 不在本文范围 |

## 3. 为什么不抄「让模型写代码」

扣子的产物是代码，表达力没有上限；PAP 的产物是图，表达力被刻意收窄：三种节点
（`agent` / `compose` / `parallel`）、五个算子（`eq` `neq` `in` `gt` `lt`）、必须显式写兜底边、
`max_nodes` 上限 12（顶 20）。这些限制是治理资产，不是欠债：

- **保存即编译**：存进库的一定是能跑的图，坏图在保存那一刻变成一句看得懂的拒绝；
- **图只排顺序，不分配能力**：每次节点调用仍然逐跳判定边界（`agent_subset`），
  所以「谁的图」不等于「谁能写」；
- **`revision` 可冻结**：在飞的 run 按旧图走完，`cursor` 不会指向一个改名的节点。

一旦允许模型生成代码，这三条同时失效——而它们正是「出事能复盘」的全部依据。
所以本文抄的是**交互形态**，不是产物形态。

## 4. 改动一：`studio_workflow_draft`（只读，不落库）

给构建助手加一个读工具：把一张图交上去，回答「能不能存、差在哪、存下来长什么样」。
**它不写库、不建审批单、不涨 `revision`**，所以可以无限次调用。

```
studio_workflow_draft(name?, definition) -> {
  ok: bool,
  errors: [{node, code, message}],             # 编译期 + 保存期的两类错误合并
  graph: {entry, nodes, edges},                # 与确认卡同一份形状
  summary: "3 步：check（field-ops）→ check2（ops）→ report（汇总）",
  preflight: {
    agents_missing: ["ops"],                   # 图里填了清单里没有的角色
    depth_hops: 2, max_nodes: 5,
    approval_nodes: ["check2"],                # 这些节点的智能体带写工具，跑到会停
    unreachable: ["check2"]                    # 模板引用了非必经路径上的节点
  },
  next: "把 errors[0] 改掉再交回来，不用重发整张图"
}
```

四条设计约束：

1. **`errors` 必须带 `node`**。`DefinitionError` 已经带（能定位时），保存期那两类
   （角色不存在、深度超限）在这里补齐。没有 `node` 的问题，模型只能整份重写。
2. **`preflight` 查库，但只读**。`studio_describe resource=agent` 那一趟省不掉，
   否则「图里写了 ops，而 ops 不存在」要等到保存时才暴露。
3. **`next` 写在返回尾部**。模型对「工具刚告诉它的下一步」的服从度高于系统提示
   （已在附件场景实测过），把「不要重发整张图」放在这里，比写进提示词三遍有用。
4. **不落库的代价**：跨轮只剩摘要。所以 `definition` 要**原样回显**在结果里，
   并且 `chat_service` 落库时那份显式字段清单（`add_message(metadata={...})`，
   在 `persist.assistant_message` 那一段）要加上 `workflow_draft`——
   节点写进 `response_metadata` 不等于落库，漏加这一行就是「当场有、刷新没有」。

工具名带 `studio_` 前缀，会由 `_studio_tool_names()` 自动进构建助手的边界，
**不要**加进 `STUDIO_BUSINESS_READ_TOOLS`（那是业务读工具名单，有单测钉形状）。

## 5. 改动二：`studio_workflow_save` 的 `patch` 参数

**不新增写工具。** `STUDIO_ASSISTANT.md` §4.2.1 已经论证过「一个写工具 + 一个删工具」，
再加一个 `studio_workflow_patch` 会让审批、摘要、verifier、备份各长出第二套。
改成同一个工具的可选参数，与 `definition` 二选一：

```json
{
  "name": "pressure-triage",
  "patch": [
    {"op": "upsert_node", "node": {"id": "check", "agent": "field-ops",
      "task": "查规程里对这类泄漏的处置要求",
      "emit": {"field": "level", "values": ["泄漏", "正常"]},
      "next": [{"when": {"key": "level", "op": "eq", "value": "泄漏"}, "to": "advise"},
               {"to": "report"}]}},
    {"op": "remove_node", "node": "diagnose"},
    {"op": "set_entry", "entry": "check"},
    {"op": "set_max_nodes", "max_nodes": 6}
  ]
}
```

语义只有三条，每条都可测：

| 规则 | 为什么 |
|---|---|
| 没给的字段**保留**；传 `{}` / `[]` 是显式清空 | 「漏传 = 清空」会让一次改名删掉整张图（这是 `definition` 那条路径已经避开的坑） |
| `next` 给就**整组替换** | 边是集合，逐条增删会留下半条边；整组替换的语义与 `save` 一致 |
| `remove_node` 连带删掉所有指向它的边，**不改接** | 指向不存在的节点（`WORKFLOW_DANGLING_EDGE`）比缺兜底边（`WORKFLOW_NO_FALLBACK`）更难修；但自动把 `A→X→B` 折成 `A→B` 是猜，会静默改变行为 |

落点：`workflow_service.save_workflow(..., patch=None)`。**注意执行顺序变了**——
现在先 `compile_definition` 再进事务，而 patch 要读库里的当前图，所以编译必须挪进事务内。
`_ensure_agents_available` 本来就在事务里，这一点不变。

`patch` 只在流程**已存在**时可用；新建必须给 `definition`（没有底图可施）。
两者同时给 → `WorkflowError`，不猜谁优先。带 `patch` 时 `mode` 可省（有底图即 `update`）。

`_preflight_workflow` 与 `summarize` 都要跟着改：
- preflight 对 patch 先查一次当前图，施加后编译——否则「注定被拒的确认卡」又回来了；
- 确认卡正文对 patch 只说**改了哪几处**（从参数就能算），不重述整张图：
  「修改流程「pressure-triage」：更新 check、删除 diagnose、入口改为 check」。
  图仍然由 `graph` 给——同一个工具、同一个渲染路径。

## 6. 改动三：回滚（P1，借 `write_backups`）

`revision` 只有号，没有快照，所以「回到第 3 版」现在做不到。**不新建版本表**：
`studio_workflow_save` 的 `_backup_workflow` 钩子已经在每次保存前把整份 `definition`
存进 `write_backups`（含 `resource_id` 与 `payload`）。

回滚 = 从 `write_backups` 取某一份 `definition` → 走一次正常的 `save`：

- **回滚是一次新的保存**，`revision` 继续涨，审计里是 `workflow_updated`。
  「版本号退回去」等于声称那段时间没发生过，而运行记录上记的是它——这条与
  `studio_agent_restore` 的口径一致（恢复类不能拿版本号当证据）；
- 备份是「保存**前**」的，所以「回到上一次的样子」是按 `created_at` 倒序取第一条，不是当前行；
- 前置条件：`write_backups` 要有保留期清理，否则这个入口会翻出三个月前的图。
  清理与「控制台展示备份设置」是同一条待办。

## 7. 试运行：不造假跑

扣子的「试运行」是**真跑**（真调模型、真调工具、真花钱），我们有 `/workflow <name> <输入>`。
不做「假跑」（只走图不调智能体），理由两条：

1. 假跑的结论是假的——每个节点的输出要靠编，而用户会拿它当证据；
2. 假跑要跳过工具调用，等于绕开审批：写节点在假跑里「成功」了，用户以为流程能跑通。

所以「试运行」= `draft` 的静态预检 + 真跑一次。真跑的节点流水（`nodes_json`）与黑板
已经在运行页可见，缺的只是**在对话里回显**：跑完把每条节点的耗时与输出前 N 字贴回会话
（助手消息的 `metadata.workflow` 已经记了流程名，只差内容）。

## 8. 入口：`/build`

**不要复用 `/workflow` 命名空间。** `parse_command` 的第二个词一定是流程名，
`/workflow new ...` 会被当成「跑一条叫 new 的流程」，而 `new` 恰好是合法名字。

`/build` 是**界面快捷方式**，不是平台命令：

- 前端 `sendChat()` 见到 `/build ` 前缀 → 不发 chat 请求，直接 `setStudioOpen(true)`
  并把原话预填进构建助手的输入框（面板本身就在同一个页面，成本是一次开关）；
- 后端仍要认识它：万一被客户端直接发过来（`/build` 不是 `/workflow`，会当普通消息交给模型，
  模型只会配合地编一段），`chat_service` 返回 409 + 一句「这条要在构建助手里做」，
  不落库、不进历史。**平台做过什么由平台说，不由模型编。**

默认助手的提示词加一句：识别到「有先后顺序、多角色接力、按条件分流」时，说明这是编排、
并提示「输入 /build 或打开右下角构建助手」——**不给默认助手任何写工具**。
身份边界按会话记（`_channel_of`），同一个 thread 里换人会让「这条会话是谁」失去意义。

### 8.1 为什么是「跳一跳」而不是「同一个会话里换人」

| 路 | 做法 | 代价 |
|---|---|---|
| A（本文） | `/build` 一跳，落到构建助手面板 | 用户多一次视线移动 |
| B | 给默认助手 `studio_workflow_save` | 默认助手是被诱导性输入说服概率最高的角色；等于开一条不经审批的配置写入。**否决** |
| C | 主对话页加「编排模式」，UI 复用面板 | 两套输入框、两套历史、两套「我刚说了什么」；收益只有省掉一次视觉跳转 |

A 的代价是可见的、C 的代价是长期的，B 的代价是治理上的。选 A。

## 9. 一轮对话走查

用户：`压力高了先让现场查规程，再让运维决定关不关阀，最后汇总给我`

1. `studio_describe resource=agent` → 有哪些角色（`field-ops` / `ops` / `assistant`）
2. `studio_workflow_draft`（第一版：check 只在「泄漏」时去 advise，**忘了兜底边**）
   → `ok=false`，`errors=[{node:"check", code:"WORKFLOW_NO_FALLBACK"}]`；说明「其余情况没有去处」
3. 模型**只给** check 补一条兜底边再交一次
   → `ok=true`，`preflight.approval_nodes=["advise"]`（`ops` 带写工具）
4. 模型把图 + 3 步 + 「第 2 步会停在审批」说给用户；用户说「就这样」
5. `studio_workflow_save`（`definition=那张图`）→ 确认卡：图 + 角色 + 分流
6. 批准 → `verify_workflow_saved` 回读（节点集合一致、`revision`、`enabled`）→ 给触发方式
7. 后续「把第 2 步换成 assistant」→ `studio_describe` 读原图 → `patch`（一个 `upsert_node`）
   → 确认卡只说「更新 advise」，其余节点一个字不动

第 3 步是这套改动真正省下来的东西：**一次改动 = 一个节点**，而不是一整份 JSON。

## 10. 边界（都不动）

| 不变量 | 为什么不能动 |
|---|---|
| 写工具只在构建助手 | 默认助手最容易被动过嘴，给它 `studio_workflow_save` 等于开一条不经审批的配置写入 |
| 起草不落库、不要审批 | 起草失败是常态；对失败也弹确认，人会对确认卡闭眼 |
| `patch` 仍然是「保存一张完整的图」 | 服务端施加 ops 后整图编译，`revision` 语义、冻结、审计都不变 |
| 校验只有一份 | `compile_definition` 是唯一权威；draft 与 preflight 都只是它的包装 |
| 角色替换必须说出来 | 用户点名 `ops`、图里用了 `field-ops` 时，**调用前**要用一句话说明换成了谁（卡上列得出用了谁，列不出他原本说了什么） |

## 11. 分期

| 期 | 范围 | 出口条件 |
|---|---|---|
| P0 | `studio_workflow_draft` + 提示词收口 | 模型在一轮里至少调用 2 次 draft，且第 2 次只改出错的那个节点 |
| P0 | `/build`（前端截住 + 后端 409 兜底） | 对话页输入 `/build ...` 能到面板；直接打 API 得到 409 而不是一段编出来的话 |
| P1 | `save` 的 `patch` | 「把第 2 步换成 X」不改动其余节点（节点集合与边逐条对比） |
| P1 | 草稿跨轮（`response_metadata.workflow_draft`） | 刷新后第二轮仍能引用上一轮的图，且 `messages` 行里真有这个字段 |
| P2 | 备份取回（回滚） | 从 `write_backups` 取回第 N 份，`revision` 继续涨而不是回退 |
| P2 | 对话里回显 run 的节点流水 | 跑完在会话里看得到每步耗时与输出前 N 字 |

## 12. 验收用例

每条去掉实现后必须失败。

1. 一张缺兜底边的图交给 `draft` → `ok=false`，`errors[0].node` 是被指出的那个节点，
   且**库里没有新增行**、`revision` 没变。
2. `draft` 里写了清单上没有的角色 → `preflight.agents_missing` 非空；
   `save` 同一张图 → 被拒，理由点名那个角色。
3. `patch` 只给 `{"op":"upsert_node","node":{"id":"check",...}}` → 其余节点的
   `task` / `emit` / `next` 与改动前逐字相同。**（反向：把 patch 实现成整图替换，这条绿）**
4. `patch` 传 `"emit": {}` → 该节点的 `emit` 被清空，不是「保留原值」。
5. `remove_node` → 指向它的边消失；没有自动改接；若某节点因此缺兜底边，
   `save` 报 `WORKFLOW_NO_FALLBACK` 并指出该节点。
6. `patch` 与 `definition` 同时给 → 被拒（`WorkflowError`），`revision` 不变。
7. `patch` 打在不存在的流程上 → 拒绝，提示「新建要给 definition」。
8. 直接 `POST /api/v1/chat` 发 `/build ...` → 409，`messages` 表没有新增行。
9. 回滚：`write_backups` 里取第 N 份 → 保存后 `revision` = 原值 + 1，审计是
   `workflow_updated`，且 `definition` 与第 N 份逐字段相等。
10. `test_studio_tool_verifiers` 仍绿：`studio_workflow_save` 有 `verifier` 与 `summarize`，
    `studio_workflow_draft` **不**需要 `verifier`（它不写）。

## 13. 相关文件

- `app/orchestration/definition.py`：`compile_definition`、`DefinitionError.node`、`WorkflowPlan`
- `app/services/workflow_service.py`：`save_workflow`（加 `patch` 参数，编译挪进事务）
- `app/tools/builtins/studio_admin_tools.py`：`studio_workflow_save` / `_preflight_workflow` /
  `_summarize_workflow_call` / `verify_workflow_saved` / `_backup_workflow`
- `app/tools/builtins/studio_tools.py`：新工具 `studio_workflow_draft` 的注册位置
- `app/services/agent_service.py`：`STUDIO_INSTRUCTION` 的编排段（draft 先行的措辞）
- `app/services/chat_service.py`：`/build` 的 409 兜底、`persist.assistant_message`
  那段元数据清单（加 `workflow_draft`）
- `static/console/assets/console.js`：`sendChat()` 的前缀截获、`setStudioOpen(true)` 与预填
