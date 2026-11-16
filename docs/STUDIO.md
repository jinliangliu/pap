# LangGraph Studio 观测

用 `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024` 观察本服务的
执行图、节点入参出参、中断（interrupt）与恢复过程。

关键点：**Studio 是浏览器直接连 `127.0.0.1:2024` 的**。
服务跑在服务器上，所以本机必须有一条 SSH 隧道把服务器端口映射到本地回环，
而不是把 2024 暴露到公网（该服务无认证）。

```
浏览器 (本机)                       服务器 203.0.113.10
┌──────────────────────┐            ┌────────────────────────────┐
│ smith.langchain.com  │  fetch     │                            │
│   ?baseUrl=          │───────────▶│  127.0.0.1:2024            │
│   127.0.0.1:2024     │            │  langgraph dev（无认证）    │
└──────────────────────┘            └────────────────────────────┘
          │                                      ▲
          └──── ssh -L 2024:127.0.0.1:2024 ──────┘
                 （本机 2024 → 服务器 2024）
```

## 1. 服务器：启动 Studio 服务

一次性准备（`studio` 是独立依赖组，不污染运行时依赖）：

```bash
cd /personal-agent-platform
/root/.local/bin/uv sync --group studio
```

> `uv sync` 默认只装 `dev` 组。后续再执行 `uv sync`（不带 `--group studio`）会把
> `langgraph-cli` 卸掉，Studio 随之起不来；重装即可，不影响 API 服务。

装成 systemd 服务，开机自启、断线不影响：

```bash
systemctl enable --now personal-agent-platform-studio
systemctl status personal-agent-platform-studio --no-pager -n 5
ss -lntp | grep 2024          # 应显示 127.0.0.1:2024，不能是 0.0.0.0
```

服务由 `deploy/systemd/personal-agent-platform-studio.service` 定义，
等价于：

```bash
uv run --no-sync langgraph dev --host 127.0.0.1 --port 2024 --no-browser --no-reload
```

## 2. 本机：建立 SSH 隧道

Windows（PowerShell / Git Bash 均可，系统自带 OpenSSH）：

```powershell
ssh -N -L 2024:127.0.0.1:2024 root@203.0.113.10
```

macOS / Linux 同上。几个要点：

- `-N` 表示只做端口转发，不开远程 shell；命令会挂住，这就是隧道在工作的表现。
- 建议带上保活与快速失败参数，否则空闲几分钟后容易被网络中间设备重置：

  ```bash
  ssh -N -o ServerAliveInterval=15 -o ServerAliveCountMax=6 \
         -o ExitOnForwardFailure=yes \
         -L 2024:127.0.0.1:2024 root@203.0.113.10
  ```

  出现 `Connection reset by peer` 就是隧道断了，重跑上面这条即可。
- 想丢到后台：Windows 用 `Start-Process ssh -ArgumentList '-N','-L','2024:127.0.0.1:2024','root@203.0.113.10'`；
  macOS/Linux 用 `ssh -fN -L 2024:127.0.0.1:2024 root@203.0.113.10`。
- 频繁使用建议配免密登录，省掉每次输密码：

  ```bash
  ssh-keygen -t ed25519          # 已有 ~/.ssh/id_ed25519 可跳过
  ssh-copy-id root@203.0.113.10 # Windows 无 ssh-copy-id 时见下方等价写法
  ```

- `-L` 的第二个地址是**服务器侧**的地址。写成 `2024:localhost:2024` 也可以，
  但 `127.0.0.1` 更明确，避免部分系统把 `localhost` 解析成 `::1` 而服务只监听了 IPv4。

## 3. 打开 Studio

浏览器访问：

```
https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

首次需登录 LangSmith，然后在图的输入框里发消息即可，能看到每个节点的
输入/输出、状态增量、interrupt 与恢复。

图标识：`personal_agent`（见 `langgraph.json`）。

### 为什么 https 页面能连 http 的 127.0.0.1

Chrome / Edge / Firefox 把 `127.0.0.1`、`localhost` 视为可信来源，
不按混合内容拦截，这是 LangGraph Studio 官方推荐用法。

## 4. 本机验证隧道是否通

不必等浏览器：

```bash
curl -s http://127.0.0.1:2024/ok
curl -s -X POST http://127.0.0.1:2024/assistants/search \
  -H 'Content-Type: application/json' -d '{}'
```

`/ok` 返回 `{"ok":true}`；`assistants/search` 里能看到 `graph_id=personal_agent`
及其元数据（注意 `/assistants` 是 GET 不允许，返回 405，别拿它判断隧道通不通）。

跑一次真实会话：

```bash
TID=$(curl -s -X POST http://127.0.0.1:2024/threads \
        -H 'Content-Type: application/json' -d '{}' \
      | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")

curl -s -X POST "http://127.0.0.1:2024/threads/$TID/runs/wait" \
  -H 'Content-Type: application/json' \
  -d '{"assistant_id":"personal_agent",
       "input":{"messages":[{"type":"human","content":"审批有效期是多少秒？"}]}}'
```

返回里 `intent=knowledge`、`citations` 非空，说明模型、知识库、数据库在隧道另一侧都正常。

## 5. Studio 里的身份与数据边界

Studio 端只有对话输入，缺少 `tenant_id` / `user_id` / `roles` / `scopes`，
因此 `app/agents/studio.py` 在 START 后插入 `prepare_context` 节点补齐这些字段：

- 身份取自服务端 `.env` 的 `DEV_SUBJECT`（默认 `pap-admin`），**不是** Studio 输入，
  与 API 同一条规则：客户端声明一律忽略；
- 该身份必须已存在（`python -m scripts.seed_dev_data` 建立过），否则节点直接报错，
  提示缺哪一步；
- 每条新消息会生成新的 `run_id`。同一线程里第二次写操作必须使用新的审批幂等键，
  否则会命中上一轮已完成的审批记录。

由此带来的影响，使用前需要知道：

| 行为 | 说明 |
|---|---|
| 写操作审批 | 会产生**真实**的 `approval_requests` 与审计记录（工具本身是 mock，不触达外部系统） |
| 会话记录 | `conversations` / `messages` 不写：那是 `chat_service` 的职责，Studio 不经过它 |
| thread 存储 | Studio 的 thread 存在 `.langgraph_api/`，与生产会话库相互独立 |
| checkpoint | Studio 用 `langgraph dev` 自带的持久化，不写生产的 PostgreSQL checkpoint 表 |

### MCP 工具要自己注册一次（否则只看得见本地工具）

生产的 MCP 工具在 `main.lifespan` 里注册（`register_mcp_tools`），而
`langgraph dev` **只导入 `graph` 这个变量，从不执行 FastAPI 的 lifespan**。
所以 `studio.py` 里的 `_ensure_mcp_tools()` 会在首次 `prepare_context` 时补注册一次，
进程内只做一次，远端不可达时只记日志、不阻塞会话。

漏掉这一步的典型症状：在 Studio 里问「T002509008 最近一天的压力趋势」，返回
`error_category: tool_rejected`、`final_response: 当前资源不可访问。`，
`planned_tool_name` 是 `get_station_snapshot` 而不是 `prv_pressure_trend`。

原因是两条 ID 体系不能互相校验：注册表里没有 `prv_*`，模型只能在本地工具里挑
`get_station_snapshot`，把 MCP 的站点码 `T002509008` 当作 `station_id` 传进去，
再被 `station_grants`（里面是 `station-001` 这种内部 ID）判成无权访问。
`mcp_tools.py` 的模块注释已经写明：MCP 工具**不设** `station_id_argument`，
站点可见范围由令牌的 `allowed_stations` 决定，不该拿 PAP 的授权表去卡。

自检：

```bash
journalctl -u personal-agent-platform-studio -n 50 --no-pager | grep studio_mcp_registered
# {"event": "studio_mcp_registered", "registered": 21}
```

## 6. 调用链看哪里

LangSmith 已于 2026-11 退役。调用链改由控制台「**观测**」页提供（自研埋点，
数据留在自己的库里、按租户隔离）。
Studio 现在只负责**图形调试**：看节点进出的输入/输出、状态增量、interrupt 与恢复。

### 不要再把 LangSmith 加回去

`.env` 里已删除 `LANGSMITH_*` 与 `LANGCHAIN_TRACING_V2`。这两个变量是
langchain-core 的**未声明外发通道**：只要存在，每一次图执行都会连同完整 prompt
推到 `api.smith.langchain.com`，不会报错，日志里也不显眼。
排障时不要「临时加回去」，用观测页。

自检有没有残留：

```bash
grep -E 'LANGSMITH|LANGCHAIN_TRACING' /personal-agent-platform/.env
# 无输出即为正常
```

### Studio 里跑的运行会进观测页吗

不会。观测的根 trace 由 `chat_service` 建立，Studio 入口不经过它。
要在观测页看到数据，请走控制台的对话接口。

这是有意的：观测的租户 / 用户语义来自 `Identity`，Studio 里没有真实身份，
记下来的 trace 分不清是谁跑的，反而是噪音。


## 7. 排障

| 现象 | 原因与处理 |
|---|---|
| Studio 显示连不上 127.0.0.1:2024 | 隧道没起：本机 `curl http://127.0.0.1:2024/ok` 验证；再看服务器 `systemctl status personal-agent-platform-studio` |
| `curl` 立刻被拒 | 服务器侧服务没起，或起在别的端口：`ss -lntp \| grep 2024` |
| 本机端口被占 | `netstat -ano \| grep :2024` 找到残留 ssh 进程并结束，再重连 |
| 图导入报 `ModuleNotFoundError` | 依赖没同步：`uv sync --group studio` |
| 节点报"需要已存在的开发身份" | 该 `DEV_SUBJECT` 未建号：`DEV_SUBJECT=pap-admin DEV_STATION_ID=station-001 uv run python -m scripts.seed_dev_data` |
| 写操作一直停在 interrupt | 这是设计如此：在 Studio 里恢复时传 `{"action": "confirm"}`（取消传 `{"action": "cancel"}`）；生产环境走 `POST /api/v1/approvals/{id}/confirm` |
| 想看某一轮的调用链 | LangSmith 已退役。用控制台「观测」页（需在「运行配置」里开着观测，默认开）；Studio 只管图形调试，见第 6 节 |
| 改了代码没生效 | 单元用了 `--no-reload`：`systemctl restart personal-agent-platform-studio` |
| 查询返回 `tool_rejected` / 「当前资源不可访问。」 | Studio 进程没注册 MCP 工具，模型退而选中本地 `get_station_snapshot`：见第 5 节末尾 |
| 写操作报 `ProgrammingError` / 审计表 RLS 报错 | Studio 缺租户上下文：升级到含 `_tenant_bound` 的版本，`execution_status` 才会正常进入 interrupt |
| 隧道几分钟后断开 | 网络中间设备重置长连接，带 `ServerAliveInterval=15` 重连即可 |

## 8. 安全红线

- **不要把 `--host` 改成 `0.0.0.0`**：该接口无认证，等于公开图结构、数据库读写与工具调用。
- **不要用 `langgraph dev --tunnel`**：会用 Cloudflare 生成公网地址，绕过本机隧道模型。
- 需要多人共用时，在前面加一层带认证的反代，并显式关闭 `/assistants` 之外的写入接口；
  当前部署不提供这种共享模式。
