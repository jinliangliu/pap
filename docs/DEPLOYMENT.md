# 部署说明

面向单机部署（Ubuntu 22.04+ / systemd / 本机 Docker PostgreSQL）。多实例与 Kubernetes 不在本文范围。

## 1. 部署形态

```
公网 ──▶ Nginx :80 ──▶ uvicorn 127.0.0.1:8000 ──▶ PostgreSQL 127.0.0.1:5432
          │                    │                       （业务表 + 知识切片
          │                    │                        + LangGraph checkpoints）
          │                    └──▶ Redis 127.0.0.1:6380
          │                          工具限流计数（跨进程共享）
          └─ HTTP Basic 认证，注入 X-Authenticated-Subject
```

三条硬约束，缺一不可：

1. **应用只监听回环地址**，外部流量必须经 Nginx。
2. **Nginx 覆盖式写入身份头**（`proxy_set_header`），客户端自带的同名头被替换掉。
3. **`AUTH_MODE=gateway`**。这是唯一让第 2 条生效的配置：dev 模式下应用会信任客户端提交的
   `X-Authenticated-Subject`，经反代暴露到公网等于没有认证。

`APP_ENV=production` 时若 `AUTH_MODE` 不是 `gateway`，应用会直接拒绝启动（见
`app/core/config.py` 的 `_validate_auth_mode`）。

## 2. 前置条件

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.12 | `.python-version` 指定 |
| uv | ≥ 0.9 | 依赖与运行入口 |
| PostgreSQL | 14+ | 业务库；本机 Docker 或独立实例均可 |
| Redis | 6+ | 工具限流计数。多进程/多副本时必需，生产环境缺失会拒绝启动 |
| Docker + gVisor | 20.10+ / runsc | 代码沙箱。每执行一个一次性容器，安装见 `deploy/sandbox/install-runsc.sh` |
| 代码沙箱 | — | 可选，独立服务，只监听回环。不配置则不注册 `execute_code`，见 [SANDBOX.md](SANDBOX.md) |
| Nginx | 1.20+ | 反向代理与基础认证 |
| apache2-utils | — | 生成 htpasswd（仅部署时需要） |

## 3. 目录与关键文件

```
/personal-agent-platform
├── .env                     # 唯一密钥来源，权限 600，不入库
├── pyproject.toml / uv.lock # 依赖锁定
├── app/                     # 应用代码
├── alembic/versions/        # schema 唯一来源
├── scripts/
│   ├── seed_dev_data.py     # 建开发身份与站点授权
│   └── ingest_knowledge.py  # 构建知识索引
├── deploy/
│   ├── systemd/personal-agent-platform.service
│   └── nginx/personal-agent-platform.conf
└── data/
    ├── knowledge/           # 知识源文件（md/txt/pdf/docx/html）
    └── index/chunks.json    # 可选导出快照；运行时检索不读它
```

会话状态与知识索引都不落本地文件：

- `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`：LangGraph 会话现场；
- `documents` / `document_chunks`：知识切片，`knowledge_index_state` 记录索引版本号；
- 三组表与业务表同库，随 PostgreSQL 一起备份即可。

## 4. 首次部署

```bash
# 4.1 代码与依赖
cd /personal-agent-platform
uv sync

# 4.2 数据库角色：应用只做 DML，DDL 归管理账号
#
# 应用账号必须是普通角色。如果 DATABASE_URL 用的是超级用户或 BYPASSRLS 角色，
# PostgreSQL 的 Row Level Security 会被整体跳过——没有任何报错，只是查得到全部租户的数据。
psql -U <管理账号> -d personal_agent <<'SQL'
CREATE ROLE pap_app LOGIN PASSWORD '<强密码>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE personal_agent TO pap_app;
GRANT USAGE ON SCHEMA public TO pap_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pap_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pap_app;
-- 迁移将来新建的表会自动继承这些权限
ALTER DEFAULT PRIVILEGES FOR ROLE <管理账号> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pap_app;
ALTER DEFAULT PRIVILEGES FOR ROLE <管理账号> IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pap_app;
SQL
# alembic_version 的权限在 4.4 建表之后再收回（此时表还不存在）

# 管理账号连接串单独存一份 root-only 文件，迁移与 checkpointer 初始化用它
printf '%s\n' 'postgresql+psycopg://<管理账号>:<密码>@127.0.0.1:5432/personal_agent' \
  > /root/.pap-admin-db-url
chmod 600 /root/.pap-admin-db-url

# 4.3 配置
cp .env.example .env
chmod 600 .env
# 必填：DATABASE_URL（用 pap_app）、OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL、AUTH_MODE=gateway
# 生产必须 CHECKPOINT_AUTO_SETUP=false（应用的 DML 角色建不了表）

# 4.4 建表（schema 只从这里来，不要手工 CREATE TABLE）
ADMIN_URL="$(cat /root/.pap-admin-db-url)"
DATABASE_URL="$ADMIN_URL" uv run alembic upgrade head
DATABASE_URL="$ADMIN_URL" uv run alembic current        # 应显示 (head)
psql -U <管理账号> -d personal_agent -c 'REVOKE ALL ON alembic_version FROM pap_app;'

# 4.5 checkpointer 三张表（幂等；同样需要 DDL 权限，故用管理账号离线跑）
uv run python -m scripts.init_checkpointer --url "$ADMIN_URL"

# 4.6 身份与授权
DEV_SUBJECT=pap-admin DEV_STATION_ID=station-001 uv run python -m scripts.seed_dev_data

# 4.7 知识索引（写入 PostgreSQL 的 documents / document_chunks）
uv run python -m scripts.ingest_knowledge
# 需要一份 JSON 快照时加 --export-json；需要指定租户时加 --tenant <id>
# 入库后各实例按索引版本号自动重载，无需重启

# 4.8 systemd
cp deploy/systemd/personal-agent-platform.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now personal-agent-platform
systemctl status personal-agent-platform --no-pager

# 4.9 巡检告警（每分钟一次，检查 7 项，异常推群机器人）
# 告警通道写在 /etc/pap-watchdog.env，与业务 .env 分开；不配通道则只写 journal。
install -m 0644 deploy/systemd/pap-watchdog.service /etc/systemd/system/
install -m 0644 deploy/systemd/pap-watchdog.timer /etc/systemd/system/
install -d -m 0750 /var/lib/pap-watchdog
install -m 0600 deploy/pap-watchdog.env.example /etc/pap-watchdog.env   # 已有则跳过
systemctl daemon-reload && systemctl enable --now pap-watchdog.timer
# 巡检项在 scripts/watchdog.py 的 CHECKS 里
```

## 5. Nginx 反向代理

```bash
apt-get install -y nginx apache2-utils

# 账号名即 users.external_subject，必须与已建身份一致
htpasswd -c /etc/nginx/pap.htpasswd pap-admin

cp deploy/nginx/personal-agent-platform.conf /etc/nginx/sites-available/personal-agent-platform
ln -sf ../sites-available/personal-agent-platform /etc/nginx/sites-enabled/personal-agent-platform
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx
```

新增用户：`htpasswd /etc/nginx/pap.htpasswd <subject>`（不要加 `-c`，否则会清空文件），
随后确保该 subject 在 `users` 表中存在——`gateway` 模式不会自动建号。

## 6. 验证清单

```bash
# 未认证必须 401
curl -s -o /dev/null -w '%{http_code}\n' http://<公网IP>/api/v1/tools

# 带认证必须 200
curl -s -o /dev/null -w '%{http_code}\n' -u pap-admin:<密码> http://<公网IP>/api/v1/tools

# 就绪检查（本机）
curl -s http://127.0.0.1:8000/ready
# 期望 database / checkpointer / tool_registry / rate_limiter 均为 true，
# 且 rate_limit_shared 为 true（说明限流确实走 Redis，而不是进程内计数）

# 伪造身份头必须无效：带上别人的 subject，响应仍应是 pap-admin 的结果
curl -s -u pap-admin:<密码> -H 'X-Authenticated-Subject: attacker' \
     -X POST http://<公网IP>/api/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"thread_id":"probe-1","message":"你好"}'

# /metrics 与 /docs 必须 404
curl -s -o /dev/null -w '%{http_code}\n' -u pap-admin:<密码> http://<公网IP>/metrics
```

写操作链路（审批闭环）部署后要手工跑一遍：发起一次写操作，确认审批单能开、能恢复、能驳回。

## 6.1 浏览器控制台（SSH 隧道）

平台没有独立前端。`/console/` 是一个同源静态应用，无构建步骤：

```
static/console/index.html          外壳：左侧导航（含就绪状态、身份、租户）+ 内容容器
static/console/assets/console.css  设计口径与组件样式
static/console/assets/console.js   hash 路由 + 各视图渲染
```

页面内部直接请求 `/api/v1/*`，因此不涉及 CORS，也不需要额外的跨域头。
新增页面只需在 `console.js` 的 `PAGES` 里注册，并在 `NAV` 里加入口——
**没有后端接口的能力一律渲染「待接入」并说明缺什么，不允许放假数据占位**。

Nginx 用 `alias` 把它挂到 `/console/`，认证沿用 server 级 `auth_basic`。
**该 location 内不得写 `auth_basic off`**，否则等于开了一个免认证入口。

### 退出登录在 Basic Auth 下意味着什么

认证在 Nginx，服务端**没有会话可以注销**：凭据由浏览器按「origin + realm」
缓存，活到浏览器进程结束。刷新页面、清 localStorage 都不会让它失效。

控制台侧栏底部的「退出登录」因此只有一种真正生效的做法：跳到
`http(s)://logout:logout@<host>/`，用一组错凭据把缓存覆盖掉，下一次请求浏览器
才会重新弹出登录框。点完会**再输一次账号密码**——这是预期行为，不是 bug。

换成交互式登录（OIDC / 表单 + Cookie）之后，退出才有服务端的注销点；那时
`signOut` 应改成调注销接口并清 Cookie，而不是靠覆盖凭据。

本机访问走 SSH 隧道，**转发到 80 而不是 8000**：

```bash
ssh -N -L 8080:127.0.0.1:80 pap
# 浏览器打开 http://127.0.0.1:8080/console/
```

Windows 上可以直接双击 `deploy\windows\tunnel.cmd`：脚本会查端口占用、后台起
ssh、轮询 `/health` 直到就绪再打印访问地址，失败时给出排查清单。
停止用 `tunnel.cmd stop`，查看状态用 `tunnel.cmd status`。

隧道方向容易理解反：`-L 8080:127.0.0.1:80` 里，左边的 8080 是**本机**监听端口，
右边的 `127.0.0.1:80` 是 ssh 连上服务器之后**在服务器视角**去连的地址。
ssh 客户端始终跑在本机，不会在服务器上装任何东西。

为什么要 80：8000 上跑的是应用本身，`AUTH_MODE=gateway` 不信任任何客户端
身份头，直连必然 401；`X-Authenticated-Subject` 只由 Nginx 注入。

三个常见坑：

- 浏览器配了全局代理时，要把 `127.0.0.1` / `localhost` 加进直连名单，
  否则隧道流量会被代理接管，表现为一直转圈或 502。
- 控制台无法切换租户：Nginx 固定把 `X-Authenticated-Tenant` 置空，
  这是有意的。要验证其他租户仍用 `curl -H 'X-Authenticated-Tenant: test'`。
- `-L` 默认只绑 `127.0.0.1`。写成 `0.0.0.0:8080:127.0.0.1:80` 会把这个服务
  暴露给整个局域网，不要这么写。

图执行过程的观测（LangGraph Studio）是另一条隧道（2024），见 `docs/STUDIO.md`。

## 7. 代码同步与日常运维

发版在**本地**执行，不要在服务器上手工拷文件：

```bash
deploy/sync.sh --dry-run      # 说明将要上线的提交，不做改动
deploy/sync.sh                # 推送并校验服务器已对齐
deploy/sync.sh --restart      # 推送 + 重启 + 健康检查
deploy/sync.sh --migrate      # 推送 + 停服迁移 + 重启 + 健康检查
deploy/sync.sh --verify       # 只比对本地提交与服务器工作区
```

发版的内容是**已提交的代码**，工作区里未提交的改动不会上线——要发的代码必须先提交。

链路：`git push` → 服务器裸仓库 `/root/git/personal-agent-platform.git` 的
`post-receive` 钩子（[`deploy/git-hooks/post-receive`](../deploy/git-hooks/post-receive)）
→ `git reset --hard origin/master` → 写 `.deployed-commit`。钩子日志在
`/var/log/pap-deploy.log`；服务器上直接改过的代码会被 `git stash` 保存，不会静默丢弃。

一次 push 就完成代码落盘，是因为这台机器对新建 SSH 连接有速率限制
（未认证连接堆积触达 `MaxStartups`，每个要等 `LoginGraceTime` 才释放）。
`deploy/sync.sh --tar` 是应急通道：不走 git，用 `git archive HEAD` 解包后 rsync 覆盖，
适用于服务器 git 状态损坏的场景；它固定排除 `.git`/`.venv`/`data`/`.env`，且不加 `--delete`。

也可以在服务器上直接 `git pull`，但**迁移仍要用管理账号**执行：

```bash
ADMIN_URL="$(cat /root/.pap-admin-db-url)"
DATABASE_URL="$ADMIN_URL" uv run alembic upgrade head
uv run python -m scripts.init_checkpointer --url "$ADMIN_URL"
systemctl restart personal-agent-platform
```

```bash
systemctl restart personal-agent-platform      # 重启
systemctl is-active personal-agent-platform    # 状态
tail -f /var/log/pap-api.log                   # 应用日志（systemd 输出到文件，不在 journald）
journalctl -u personal-agent-platform -n 100   # systemd 侧日志
python3 scripts/watchdog.py                    # 手工跑一轮巡检（不发告警加 --no-alert）
journalctl -u pap-watchdog | grep watchdog_checks | tail -5   # 最近几轮巡检结论
```

| 场景 | 命令 |
|---|---|
| 发新版本 | `deploy/sync.sh --restart`；带数据库迁移用 `deploy/sync.sh --migrate`（已含重启） |
| 改了知识资料 | `uv run python -m scripts.ingest_knowledge --tenant <id>`（无需重启） |
| 收到巡检告警 | 告警项名称即 `scripts/watchdog.py` 里的检查函数名，按名定位 |
| 新增迁移 | `uv run alembic revision --autogenerate -m "..."` 后检查生成内容，再用 `--migrate` 部署 |
| 查审批状态 | `SELECT status, tool_name, error_code FROM approval_requests ORDER BY created_at DESC LIMIT 20;` |
| 查审计链 | `SELECT event_type, count(*) FROM audit_logs GROUP BY 1 ORDER BY 2 DESC;` |

### SSH 连接限制

这台机器曾出现「TCP 能连上、但读不到 SSH banner、新连接被 reset」。不是防火墙（`ufw`/`fail2ban` 均未启用），是 sshd 的 `MaxStartups` 默认值 `10:30:100`：未认证连接堆到 10 个开始随机丢弃，30 个全丢，每个被丢的连接还要占着 `LoginGraceTime`(120s)。

对策有两层，都已落地：

1. `/etc/ssh/sshd_config.d/99-pap-limits.conf`（源文件 [`deploy/ssh/99-pap-limits.conf`](../deploy/ssh/99-pap-limits.conf)）把 `MaxStartups` 提到 `30:50:100`；另设 `ClientAliveInterval 60` + `ClientAliveCountMax 3`，让真正失联的连接约 3 分钟后被回收；
2. 发版压到一次连接（`git push` + `post-receive` 钩子），脚本里的 ssh/push 都带退避重试。

实测：连续 5 次新建连接由「必断」变为 5/5 成功。

#### 客户端侧

服务器上的 `ClientAliveInterval` 只探测**链路是否存活**：探测包由 ssh 进程自己回应，不需要应用层有输出。实测关掉客户端保活、跑 200 秒零输出的命令（超过 3 个探测周期）连接仍然存活，所以它不会打断 `tail -f` 这类长任务。

客户端保活解决的是另外两件事：防止中间 NAT/防火墙回收空闲 TCP 会话（这类设备常见 5–30 分钟超时），以及及早发现断链（不必等 TCP 自己超时）。建议写进本地 `~/.ssh/config`：

```
Host pap
    HostName <服务器 IP>
    User root
    ServerAliveInterval 15
    ServerAliveCountMax 4
    TCPKeepAlive yes
    ConnectTimeout 20
```

之后 `ssh pap`、`scp pap:/path .`、`rsync -e ssh pap:...` 都自动带上保活。

## 8. 升级与回滚

升级前必须备份：

```bash
tar czf /root/pap-backup-$(date +%Y%m%d-%H%M%S).tar.gz \
    -C / personal-agent-platform \
    --exclude=personal-agent-platform/.venv
docker exec personal-agent-postgres pg_dump -U agent personal_agent | gzip > /root/pg-$(date +%Y%m%d).sql.gz
```

回滚：

```bash
systemctl stop personal-agent-platform
tar xzf /root/pap-backup-<时间戳>.tar.gz -C /
uv run alembic downgrade -1     # 仅在迁移不可向前兼容时
systemctl start personal-agent-platform
```

会话状态在 PostgreSQL 里，与代码版本无关，回滚代码不需要回滚 checkpoint 表；
但 checkpoint 表结构若被新版本扩展，旧代码仍可读（迁移是增量的）。

## 9. 安全清单

- [ ] `.env` 权限 600，且确认 `git check-ignore .env` 命中
- [ ] **`DATABASE_URL` 用的是普通角色**（`rolsuper=f`、`rolbypassrls=f`），否则 RLS 整体失效
- [ ] `/root/.pap-admin-db-url` 权限 600，只给迁移与初始化用
- [ ] `CHECKPOINT_AUTO_SETUP=false`，应用运行时零 DDL
- [ ] `AUTH_MODE=gateway` 且 **`APP_ENV=production`**。写成 `development` 不会影响认证
      （gateway 模式下 dev 分支本就不生效），但会让「生产开观测」「生产用内存限流」
      「生产无沙箱密钥」这几道启动期防呆**全部静默失效**——线上曾长期是 `development`，
      防呆一次都没触发过
- [ ] 应用监听 `127.0.0.1`，不是 `0.0.0.0`
- [ ] Nginx 已删除 `sites-enabled/default`
- [ ] `/metrics`、`/docs`、`/openapi.json` 对外 404
- [ ] 防火墙只放行 22/80/443（`ufw allow 22,80,443/tcp && ufw enable`）
- [ ] 密钥已在仓库外管理；如曾出现在终端或日志中，应轮换
- [ ] PostgreSQL 端口不暴露到公网（`127.0.0.1:5432`）

## 10. 故障排查

| 现象 | 排查方向 |
|---|---|
| `401 未认证用户：缺少可信网关注入的身份信息` | 请求没经过 Nginx，或 `auth_basic` 未生效 |
| 所有请求都是同一个用户 | Nginx 未覆盖 `X-Authenticated-Subject`，检查 `proxy_set_header` 是否写成了 `$http_x_authenticated_subject` |
| 启动即退出，日志含 `AUTH_MODE=gateway` | `APP_ENV=production` 但 `AUTH_MODE=dev`，改 `.env` |
| `/ready` 中 `database: false` | PostgreSQL 未启动或 `DATABASE_URL` 密码未 URL 编码（`!`→`%21`、`@`→`%40`、`#`→`%23`、`$`→`%24`） |
| 启动即退出，日志含 `permission denied for schema public` | 应用角色没有 DDL 权限而 `setup()` 仍在跑，设 `CHECKPOINT_AUTO_SETUP=false` 并离线跑 `init_checkpointer` |
| `GET /me` 500，日志含 `KeyError: 'active'` | 枚举按成员名存值，与库里的小写 value 不匹配，检查 `Enum(values_callable=...)` |
| 换租户后查不到自己的数据 | `with_loader_criteria` 用了闭包捕获租户，被语句缓存串用 |
| `alembic upgrade` 报版本冲突 | 库里有旧版本号但代码里没有该迁移，`DELETE FROM alembic_version;` 后重新 `upgrade head` |
| 检索结果为空 | 索引未构建或资料为空文件，重跑 `ingest_knowledge` 并检查 `data/index/chunks.json` |
| 写操作一直 `EXECUTION_UNKNOWN` | 外部系统超时或结果不确定，需人工核查，系统不会自动重发 |
| 频繁 SSH 连接后被拒（`Connection closed` / `Connection reset`） | sshd 未认证连接堆积触达 `MaxStartups`（默认 `10:30:100`）后丢弃新连接，且 TCP 能握手、读不到 banner；每个残留连接要等 `LoginGraceTime`(120s) 才释放。本机已加 `/etc/ssh/sshd_config.d/99-pap-limits.conf`（`MaxStartups 30:50:100`、`ClientAliveInterval 60`），并把发版压到一次连接；`deploy/sync.sh` 的 ssh/push 都带退避重试 |
