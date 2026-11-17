# 远端部署与本地工作区比对

一份可以反复执行的巡检清单：把「远端实际在跑的」和「仓库里写的」逐项对齐，
并给出每项的判定标准。新增/升级部署后、或怀疑环境不一致时，按本文走一遍。

基线：2026-11-03，`master@17990e9`（写完本报告后又追加了 install.sh 的沙箱同步块）。

## 0. 一条命令看全貌

```bash
ssh pap "cd /personal-agent-platform && git log --oneline -1 && git status --porcelain | head && \
  systemctl is-active personal-agent-platform personal-agent-platform-studio pap-sandbox pap-watchdog.timer && \
  curl -s -m 8 http://127.0.0.1:8000/ready"
```

`status` 空、四个单元 `active`、`/ready` 全 `true`，是"没跑偏"的最小信号。

## 1. 代码与依赖

```bash
# 两侧 HEAD 必须相同，且工作区干净
cd <本地仓库> && git log --oneline -1 && git status --porcelain
ssh pap "cd /personal-agent-platform && git log --oneline -1 && git status --porcelain"

# 依赖差异（平台相关的 colorama/pywin32/uvloop 属正常）
uv pip list --format=freeze | sort > /tmp/l.txt
ssh pap "/root/.local/bin/uv pip list --format=freeze | sort" > /tmp/r.txt
comm -3 /tmp/l.txt /tmp/r.txt

# 迁移：文件数量一致，且 DB 版本号 = 迁移链头
ls alembic/versions | wc -l
ssh pap "docker exec personal-agent-postgres psql -U agent -d personal_agent -t -c 'select version_num from alembic_version;'"
```

判定：HEAD 一致；依赖差异只出现在 Windows/Unix 专属包；`alembic_version` 等于
`alembic/versions` 里无后继的那个 revision。

**换行陷阱**：Windows 侧 `core.autocrlf=true` 会把工作区文件转成 CRLF，
`git status` 仍显示干净，但 `md5sum` 与远端不一致。要比较真实内容用：

```bash
tr -d '\r' < 本地文件 | md5sum
ssh pap "md5sum 远端文件"
```

## 2. 配置（`.env`）

```bash
grep -oE '^[A-Z0-9_]+=' .env | tr -d '=' | sort -u > /tmp/lk.txt
ssh pap "grep -oE '^[A-Z0-9_]+=' /personal-agent-platform/.env | tr -d '=' " | tr -d '\r' | sort -u > /tmp/rk.txt
comm -13 /tmp/lk.txt /tmp/rk.txt   # 仅远端有
comm -23 /tmp/lk.txt /tmp/rk.txt   # 仅本地有
```

判定：本地应是远端的**子集**，缺的键集中在 `MCP_*`、`SANDBOX_*`、`REDIS_*`、
`RATE_LIMIT_*`、`DEV_*`——这些正是"本地跑不起来"的原因。共有键中只有
`DATABASE_URL` / `CHECKPOINT_DATABASE_URL` 允许不同（远端用受限账号 `pap_app`）。

## 3. 配套工具的部署副本

这一组最容易漂移：它们在仓库里，但**启动时读的是仓库外的副本**。

| 组件 | 实际读取位置 | 是否由 `install.sh` 维护 |
|---|---|---|
| systemd 单元 ×5 | `/etc/systemd/system/` | 是（本次已含沙箱） |
| Nginx 站点 | `/etc/nginx/sites-enabled/` | 是 |
| 巡检脚本 | `/personal-agent-platform/scripts/watchdog.py` | 是（原地运行） |
| **沙箱启动器** | `/opt/pap-sandbox/{server,runtime}.py` | 是（本次新增） |
| 沙箱镜像 | `pap-sandbox-runtime:1` | 否（一次性构建，见 `deploy/sandbox/build-image.sh`） |
| MCP 服务端 | 容器 `prv-mcp`（镜像 `station-mcp:1.0`） | 否（外部项目，源码不在本仓库） |

```bash
# 单元逐字比对
for f in personal-agent-platform personal-agent-platform-studio pap-sandbox pap-watchdog.service pap-watchdog.timer; do
  ssh pap "cat /etc/systemd/system/$f" | diff -q - deploy/systemd/$f && echo "$f 一致"
done

# 沙箱副本：用 git blob（LF 原文）比，避免 CRLF 干扰
for f in server runtime; do
  echo -n "$f repo: "; git show HEAD:deploy/sandbox/$f.py | md5sum
  echo -n "$f /opt: "; ssh pap "md5sum /opt/pap-sandbox/$f.py"
done
```

判定：md5 相同。不同则确认是"副本旧"还是"只是换行不同"——后者用
`diff --strip-trailing-cr` 验证。

## 4. 运行态与数据

```bash
ssh pap "systemctl is-active personal-agent-platform personal-agent-platform-studio pap-sandbox pap-watchdog.timer"
ssh pap "curl -s http://127.0.0.1:8000/ready; curl -s http://127.0.0.1:8199/v1/health; curl -s http://127.0.0.1:2024/ok"
ssh pap "docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}'"
```

判定：`/ready` 全部 `true`；沙箱 `"ready":true,"runtime":"runsc"`；三个 PAP 容器
端口全部绑在 `127.0.0.1`。

## 5. 宿主机端口暴露（跨项目，需另行确认）

**这是宿主机层面的风险，不属于 PAP 仓库范围（已于 2026-11-03 16:50 收敛，见本节末）。**

`2026-11-03` 查出四个 `docker run` 手工创建、无 compose 标签的容器把端口发布到
`0.0.0.0`：

| 容器 | 镜像 | 端口 | 创建时间 |
|---|---|---|---|
| `redis` | `redis:6.2.10` | 6379 | 2026-07-13 |
| `mysql` | `mysql:5.7.42` | 3307 | 2026-07-13 |
| `rabbitmq` | `rabbitmq:3-management` | 5672 / 15672 | 2026-07-13 |
| `prv-mcp` | `station-mcp:1.0` | 8765 | 2026-11-03 |

它们早于 PAP 部署近四个月，是别的项目的存量服务。PAP 自己的
`personal-agent-redis`（6380）与 `personal-agent-postgres`（5432）**本来就绑在
127.0.0.1**，无需处置。

现场证据：`ss -tn state established` 显示这些端口只有本机与 docker 网桥
（172.17/172.18）的连接，没有任何外部客户端；PAP 调用 MCP 走
`MCP_SERVER_URL=http://127.0.0.1:8765/mcp`，只经回环。

要收敛的话，用 `deploy/firewall/docker-port-guard.sh`。它只改 `DOCKER-USER` 链，
不碰容器、不重启服务、规则即时生效也可即时撤销：

```bash
sudo deploy/firewall/docker-port-guard.sh status          # 先看现状
sudo deploy/firewall/docker-port-guard.sh apply           # 收敛
sudo deploy/firewall/docker-port-guard.sh remove          # 回滚
sudo deploy/firewall/docker-port-guard.sh persist         # 开机自动生效
```

生效后规则依次是：`lo` 放行 → `172.16.0.0/12` 放行 → 目标端口 `DROP`。

**已处置（2026-11-03 16:50）**：执行了 `apply`，三条规则就位。核验结果：

| 项 | 结果 |
|---|---|
| 外网 6379 / 3307 / 15672 / 8765 | 全部不可达 |
| 本机既有连接（5672、6379） | 保持不变，未被误伤 |
| 容器 | 6 个全部 `Up`，PAP 两个 `healthy` |
| `/ready` | 8 项全 `true`（含 `mcp_tools`） |
| 经 PAP 网关的真实 MCP 调用 | `prv_query_pressure` 成功，返回实时压力 |

> **尚未持久化**：宿主机没装 `iptables-persistent`，重启后规则会丢。
> 需要开机自动生效时执行一次 `sudo deploy/firewall/docker-port-guard.sh persist`。

> 为什么不能用 ufw：docker publish 走 NAT `DOCKER` 链 + `FORWARD`，不经过
> `INPUT`，所以 ufw/iptables 的 INPUT 规则管不到它。必须落在 `DOCKER-USER`。

## 6. 本地为何不能复现远端行为

本地缺 24 个配置键、没有 Postgres、没有 Docker，因此下面这些在本地**测不到**：

- RLS（行级安全）与 `tenant_scope` 上下文
- 限流共享后端（Redis）
- 代码沙箱（要 runsc + 镜像）
- MCP 工具（`MCP_ENABLED` 未开，注册表里没有 `prv_*`）
- LangGraph Studio 入口（需要 `langgraph dev`）

单测跑的是 SQLite（`tests/conftest.py` 把 `DATABASE_URL` 换成 `sqlite+aiosqlite`），
所以 RLS 相关回归只能靠集成测试或远端验证。2026-11-03 那个 Studio 报
`tool_rejected` 的问题就是这一类：本地一切正常，只有在缺租户上下文的真实
RLS 环境里才暴露。

## 7. 历史问题与处置记录

| 日期 | 问题 | 处置 |
|---|---|---|
| 11-03 | `/opt/pap-sandbox/server.py` 停留在旧版，缺 `X-Sandbox-User/Conversation` 归属校验 | 用 git blob（LF）覆盖，重启验证；`install.sh` 新增沙箱同步块 |
| 11-03 | `/opt/pap-sandbox/runtime.py` 内容新但为 CRLF | 同上，统一为 LF |
| 11-03 | Studio 进程内没有 `prv_*` 工具，报 `tool_rejected` | `studio.py` 首次进入时注册一次 MCP 工具 |
| 11-03 | Studio 无租户上下文，审计写不进、审批 500 | `nodes.py` 加 `_tenant_bound` 装饰器 |
| 11-03 | 宿主机 6379 等端口暴露公网 | 已用 `docker-port-guard.sh apply` 收敛（16:50），外网不可达、业务无影响；**未启用 persist，重启后失效** |
