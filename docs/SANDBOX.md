# 代码沙箱

Agent 运行在平台进程内，但**不执行任何代码**。需要跑代码时，`execute_code` 工具
通过 HTTP 把代码交给独立的沙箱服务；沙箱为**每一次执行**创建一个一次性
gVisor 容器，拿回 stdout / stderr / 退出码。

第一阶段采用 **Docker + gVisor（runsc）**。第二、三阶段（Kubernetes + Kata、
Firecracker Worker Pool）不在当前实现范围内。

## 1. 为什么必须外置

| 做法 | 问题 |
|---|---|
| `exec()` 在 API 进程里 | 没有隔离边界，一段 `os.remove` 就能删掉服务文件；超时只能靠信号 |
| `subprocess` 在同一台机器 | 能读到 `.env`、能连内网数据库；rlimit 挡不住 fork 变种与内核提权 |
| 外置沙箱 + 一次性容器 | 平台侧只有 HTTP 客户端，逃逸影响范围被限制在容器内 |

这条边界不能靠代码规范保证，只能靠部署形态保证。

## 2. 多租户隔离模型

每次执行都是**全新容器**，容器退出即销毁，可写层随 `--rm` 一起消失。
租户之间没有共享文件系统、没有共享进程空间、没有共享网络。

| 威胁 | 手段 |
|---|---|
| 读取平台密钥 / 业务代码 | 容器**不挂载任何宿主路径**；代码经 stdin 传入；根文件系统 `--read-only` |
| 跨租户读取残留数据 | 每个执行一个容器，`--rm` 销毁；工作区是 tmpfs，不落盘 |
| 跨租户读取执行记录 | 记录按 `X-Sandbox-Tenant` 归属，跨租户回读一律 **404**（不是 403，避免泄露 ID 是否存在） |
| 数据外带 / 下载二阶段载荷 | `--network none`，容器内没有网络接口 |
| 提权 | gVisor 用户态内核 + `--cap-drop ALL` + `no-new-privileges` + 非 root（65534） |
| 资源争抢 | 每容器 `--memory` / `--cpus` / `--pids-limit`；服务端再做**每租户并发配额** |
| 宿主机目录穿越 | 不挂宿主目录，也不使用 `--privileged` / `--pid host` / `--ipc host` |

**服务端也做校验**：语言、代码大小、超时都再校验一次。平台侧的限制是体验，
服务端的限制才是边界——直接调用沙箱 API 的人不会经过平台校验。

## 3. 调用链

```
规划器判定需要执行代码
   → execute_code（risk=execute，requires_approval 默认 true）
   → 审批（PENDING → APPROVED，参数哈希绑定代码内容）
   → SandboxClient.execute(tenant_id=...)
   → POST /v1/execute   （X-Sandbox-Tenant 决定归属）
   → 一次性 gVisor 容器执行 → 容器销毁
   → GET /v1/executions/{id} 回读核验（同租户）→ VERIFIED
```

代码属于审批参数的一部分，篡改代码会让 `parameter_hash` 不匹配，confirm 直接 422。

## 4. 协议契约

### `POST /v1/execute`

```http
POST /v1/execute
Authorization: Bearer <SANDBOX_API_KEY>
X-Sandbox-Tenant: <tenant_id>
Idempotency-Key: approval:<thread_id>:<run_id>
Content-Type: application/json

{
  "language": "python",
  "code": "print(sum(range(1, 101)))",
  "stdin": "",
  "timeout_seconds": 30
}
```

响应：

```json
{"execution_id": "e-1", "status": "succeeded", "exit_code": 0,
 "stdout": "5050\n", "stderr": "", "duration_ms": 412,
 "truncated": false, "backend": "runsc"}
```

`status`：`succeeded` / `failed` / `timeout` / `cancelled`。

也支持 202 + 轮询（客户端按 `SANDBOX_POLL_INTERVAL_SECONDS` 轮询
`GET /v1/executions/{id}`），本实现直接返回终态。

### `GET /v1/executions/{execution_id}`

供轮询与**回读核验**。必须带同一个 `X-Sandbox-Tenant`，否则 404。

### `GET /v1/health`

不需要鉴权。返回运行时与镜像状态：

```json
{"status": "ok", "ready": true, "runtime": "runsc",
 "image": "pap-sandbox-runtime:1", "runtimes": ["runc", "runsc"],
 "backend": "runsc", "active_executions": 1, "max_per_tenant": 2}
```

运行时未注册或镜像缺失时返回 **503** 且 `ready=false`，`detail` 给出修复动作。
它只进 `/ready` 的 `checks.sandbox`，**不参与整体就绪判定**：
沙箱挂了不该让对话、检索、场站工具一起被判死。

## 5. 平台侧行为

| 关注点 | 行为 |
|---|---|
| 工具可见性 | 未配置 `SANDBOX_BASE_URL` 时工具**不注册**，模型看不到该能力 |
| 审批 | `SANDBOX_REQUIRES_APPROVAL=true`（默认）。关掉等于允许模型直接执行任意代码 |
| 语言 | 平台白名单与 schema enum 双重限制，沙箱侧再校验一次 |
| 代码大小 | `SANDBOX_MAX_CODE_BYTES`（默认 20000），超限不发请求 |
| 超时 | 用户值 clamp 到 `SANDBOX_TIMEOUT_SECONDS`；网关硬超时 +5s；容器外层 +5s |
| 幂等 | `Idempotency-Key` 按 `tenant + key` 存储；重复提交返回同一结果，执行中重复提交返回 409 |
| 输出 | stdout/stderr 各自截断，`truncated` 标记 |
| 密钥 | `SANDBOX_API_KEY` 只存在于服务端与服务环境文件，不进工具 schema 与模型上下文 |

## 6. 部署

```bash
# 1) 安装 gVisor 并注册 docker 运行时（需重建 docker daemon 配置）
sudo GVISOR_VERSION=release-20261026.0 bash deploy/sandbox/install-runsc.sh

# 2) 构建运行时镜像（只含解释器与执行入口）
bash deploy/sandbox/build-image.sh

# 3) 独立用户 + 独立虚拟环境 + docker 组
useradd --system --home /opt/pap-sandbox --shell /usr/sbin/nologin sandbox || true
usermod -aG docker sandbox
mkdir -p /opt/pap-sandbox
python3 -m venv /opt/pap-sandbox/.venv
/opt/pap-sandbox/.venv/bin/pip install -q fastapi uvicorn
chown -R sandbox:sandbox /opt/pap-sandbox

# 4) 运行时配置（不要写进 systemd 单元，避免密钥进 git）
install -m 0600 -o root -g sandbox /dev/null /etc/pap-sandbox.env
cat >/etc/pap-sandbox.env <<'EOF'
PAP_SANDBOX_BACKEND=runsc
PAP_SANDBOX_IMAGE=pap-sandbox-runtime:1
PAP_SANDBOX_API_KEY=<至少 32 位随机串>
PAP_SANDBOX_MAX_CONCURRENT=4
PAP_SANDBOX_MAX_PER_TENANT=2
EOF

# 5) 装单元 + 铺源码（deploy/install.sh 的沙箱块，可重复执行）
PAP_INSTALL_SANDBOX=1 bash deploy/install.sh
```

### 6.1 日常更新

**不要手工 `scp` `server.py` / `runtime.py` 到 `/opt/pap-sandbox`。**
`deploy/install.sh` 已经包含沙箱块：把 `deploy/sandbox/{server,runtime}.py`
去掉行尾 CR 后安装到 `/opt/pap-sandbox`，属主设为 `sandbox`，再重启并做健康检查。

历史上正是因为靠手工拷贝，`/opt/pap-sandbox/server.py` 曾停留在旧版本
（缺 `X-Sandbox-User` / `X-Sandbox-Conversation` 归属校验），而仓库里已经是新版。
排查手法见 `docs/DEPLOYMENT.md` §7「代码同步与日常运维」：比对远端与工作区的文件清单与行尾。

跳过沙箱（例如只更新主服务）：`PAP_INSTALL_SANDBOX=0 bash deploy/install.sh`。

平台 `.env` 保持：

```bash
SANDBOX_BASE_URL=http://127.0.0.1:8199
SANDBOX_API_KEY=<与 /etc/pap-sandbox.env 相同>
SANDBOX_REQUIRES_APPROVAL=true
```

> `PAP_SANDBOX_BACKEND=process` 只用于本地联调，没有网络、文件系统与内核隔离。
> 服务会打 `process 后端没有隔离` 警告，健康检查返回 `isolation=none`。

### 权限说明（必须理解）

启动器需要访问 `/var/run/docker.sock`，而 docker 组等价于宿主 root。
因此**启动器是受信组件，被执行的代码不是**：

- 启动器不解析模型给的任何命令行参数，`docker run` 的参数由代码硬编码；
- 容器内**不挂载** docker socket，代码无法创建新容器；
- 启动器进程 `InaccessiblePaths=/personal-agent-platform`，读不到平台 `.env`；
- 真正把"代码"和"宿主"隔开的，是 gVisor + 一次性容器，不是这个用户。

这条取舍是阶段一的固有代价。阶段二改成向 Kubernetes API 提交 Pod，
用命名空间级 ServiceAccount 与 ResourceQuota 取代 docker socket。

## 7. 验证

```bash
# 服务端就绪（应 ready=true、runtime=runsc）
curl -s http://127.0.0.1:8199/v1/health | python3 -m json.tool

# 平台 /ready 的 checks.sandbox=true
curl -s http://127.0.0.1:8000/ready | python3 -m json.tool

# 直接打协议（需要 key 与租户头）
KEY=$(grep PAP_SANDBOX_API_KEY /etc/pap-sandbox.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8199/v1/execute \
  -H "Authorization: Bearer $KEY" -H 'X-Sandbox-Tenant: tenant-a' \
  -H 'Content-Type: application/json' \
  -d '{"language":"python","code":"print(sum(range(1,101)))","timeout_seconds":5}'
```

一条命令跑完九项隔离自检（基线执行、宿主文件不可见、无出站网络、
跨租户回读 404、鉴权、租户配额、无 docker socket、非 root）：

```bash
python3 deploy/sandbox/verify_isolation.py \
  --api-key "$(grep PAP_SANDBOX_API_KEY /etc/pap-sandbox.env | cut -d= -f2)"
# 期望：9/9 通过
```

也可以手工逐条验证：

```bash
# 1) 读不到平台目录
python - <<'PY'
print(open("/personal-agent-platform/.env").read()[:20])
PY
# → FileNotFoundError / PermissionError

# 2) 没有网络
import socket; socket.create_connection(("1.1.1.1", 443), timeout=3)
# → timeout

# 3) 跨租户回读 404（用 tenant-b 读 tenant-a 的 execution_id）
```

## 8. 运维

| 项 | 说明 |
|---|---|
| 孤儿容器 | 启动时与每 60 秒按 `label=pap.sandbox=1` + `until=<TTL>` 清理；崩溃不会永久占资源 |
| 镜像升级 | `build-image.sh` 打新 tag，改 `/etc/pap-sandbox.env` 的 `PAP_SANDBOX_IMAGE` 后重启服务；旧容器不受影响 |
| 容量 | 单容器 512MB / 1 CPU / 64 进程；`PAP_SANDBOX_MAX_CONCURRENT` 要与宿主内存对齐（4 × 512MB + 余量） |
| 日志 | `/var/log/pap-sandbox.log`；容器不会把日志写到宿主 |
| 执行记录 | 内存保留 10 分钟、上限 512 条，仅用于回读核验，不是审计存储 |

## 9. 已知边界

- **gVisor 不是零信任**：它缩小了内核攻击面，不等于形式化隔离。多租户商用建议阶段二/三。
- **执行记录在内存**：进程重启后旧 `execution_id` 回读 404，审批会停在 `EXECUTED`。这是刻意的——不允许用"猜"来完成核验。部署更新时避开有在途审批的窗口。
- **单机**：容器都在同一台宿主上，节点故障会丢掉所有在途执行；跨机扩展属于阶段二。
- **语言支持**：镜像内只有 Python 与 Node。新增语言要同时改 `runtime.LANGUAGES`、`image/runner.py` 与镜像。
