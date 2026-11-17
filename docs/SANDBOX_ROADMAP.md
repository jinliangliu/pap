# 沙箱演进路线（阶段二、三为存档设计）

面向多租户、强数据隔离的代码执行能力，分三阶段演进。**只有阶段一已实现**；
阶段二、三保留设计，等触发条件成立再投入——过早引入编排层会先把运维复杂度
付掉，却拿不到对应的隔离收益。

三阶段共用同一套 HTTP 协议（`POST /v1/execute` + `GET /v1/executions/{id}` +
`GET /v1/health`）。**换阶段不改平台代码**，只换沙箱服务端实现。

## 0. 一张表看完差异

| | 阶段一 Docker + gVisor（已实现） | 阶段二 Kubernetes + Kata（存档） | 阶段三 Firecracker Pool（存档） |
|---|---|---|---|
| 隔离单位 | 容器 + 用户态内核 | Pod + 轻量 VM（Kata） | 每执行一个 microVM |
| 内核 | 共享宿主内核，系统调用经 gVisor 拦截 | 每个 Pod 独立 guest 内核 | 每个 microVM 独立 guest 内核 |
| 启动开销 | 约 0.2–0.6 s | 约 0.5–1.5 s | 约 0.1–0.3 s（快照恢复） |
| 单机密度 | 高（数十到数百） | 中（受 VM 内存限制） | 高（但需要自建调度） |
| 跨机扩展 | 无（单机） | 原生（Deployment / HPA） | 需自研 worker 调度 |
| 故障域 | 单机故障丢全部在途 | 节点故障只影响该节点 Pod | 单 VM 故障只影响一次执行 |
| 主要运维成本 | docker + gVisor 升级 | K8s 集群 + Kata + RuntimeClass | 自研控制面 + 镜像/快照管理 |
| 适用规模 | 单机、中小租户量 | 多节点、需要弹性 | 大规模、强隔离、成本敏感 |

## 1. 阶段一：Docker + gVisor（已实现）

### 结构

```
平台 ──HTTP──▶ 沙箱服务（127.0.0.1:8199）
                  │ 鉴权 / 租户 / 配额 / 幂等 / 记录
                  ▼
              docker run --rm --runtime=runsc
                  ├─ --network none
                  ├─ --read-only + tmpfs 工作区
                  ├─ --cap-drop ALL / --user 65534
                  └─ 一次性容器：执行完即销毁
```

### 关键决策

| 决策 | 原因 |
|---|---|
| 每次执行一个容器，不复用 | 复用就意味着一租户的残留文件可能被另一租户读到；预热池的收益抵不上这个风险 |
| 代码走 stdin，不挂宿主目录 | 没有宿主路径进容器，`/personal-agent-platform` 自然不可见 |
| 服务端二次校验语言/大小/超时 | 直接调用 API 的客户端不会经过平台校验 |
| 租户来自请求头，记录按租户隔离 | 跨租户回读返回 404，不泄露 ID 存在性 |
| 每租户并发配额 | 单租户不能占满全机；噪声邻居被限制在自己的额度内 |
| 健康检查探测运行时而非启动容器 | `/ready` 每分钟被调用，不能为它付一次容器启动成本 |

### 容量估算

单容器默认 512MB / 1 CPU / 64 进程。`PAP_SANDBOX_MAX_CONCURRENT=4` 时
峰值内存约 2GB，加上镜像与 docker 自身，建议宿主 ≥ 8GB。
提高并发前先按 `并发 × 512MB` 核对内存，而不是先看 CPU。

### 已接受的边界

- 单机：没有跨节点调度，节点故障丢掉在途执行；
- docker socket 等价 root：启动器是受信组件，靠"参数硬编码 + 不挂 socket 进容器"约束；
- 执行记录在内存：进程重启后回读 404，审批停在 `EXECUTED`（刻意如此，不猜）。

### 进入阶段二的触发条件（满足任意两条）

1. 需要**跨节点**扩容：单机内存/CPU 已成为瓶颈，或需要滚动升级不中断在途执行；
2. 租户数量与信任等级上升，要求"每个租户独立 guest 内核"而不是共享 gVisor 内核；
3. 出现**多团队共用**的诉求，需要按命名空间做配额、审计、网络策略；
4. 单机故障已造成业务可见损失，需要把故障域缩小到单次执行。

## 2. 阶段二：Kubernetes + Kata Containers（存档，不实现）

### 目标

把"一次性容器"换成"一次性 Pod"，由 K8s 负责调度、配额、滚动升级；
隔离从 gVisor 的共享内核升级为 **每个 Pod 一个 guest 内核**（Kata）。

### 参考结构

```
平台 ──HTTP──▶ 沙箱网关（Deployment，2+ 副本）
                  │ 鉴权 / 租户 / 配额 / 记录（存 PG 或 Redis）
                  ▼
              Kubernetes API（不再有 docker socket）
                  ▼
              Pod（RuntimeClass=kata-fc）
                  ├─ NetworkPolicy: deny-all
                  ├─ ResourceQuota / LimitRange（按 namespace）
                  ├─ readOnlyRootFilesystem + emptyDir 工作区
                  └─ securityContext: runAsNonRoot / drop ALL / seccomp
```

### 设计要点

| 项 | 做法 |
|---|---|
| 租户隔离 | 每个信任域一个 namespace；ResourceQuota 限制总量，LimitRange 限制单 Pod |
| 执行隔离 | 一执行一 Pod，`activeDeadlineSeconds` 兜底；结束即删，`emptyDir` 随 Pod 消失 |
| 运行时 | RuntimeClass `kata-fc`（Firecracker VMM）或 `kata-qemu`；节点用 taint 隔离沙箱专用池 |
| 控制面 | 平台不再持有 docker socket，改为 ServiceAccount + 最小 RBAC（create/delete pod、读 log） |
| 网络 | 默认 deny-all NetworkPolicy；需要出网时走显式白名单 egress |
| 结果回传 | Pod 退出码 + 日志（`pods/log`）；执行记录写 PG，网关无状态可水平扩展 |
| 冷启动 | 节点预热 pause Pod / 镜像预拉取；接受 0.5–1.5 s |
| 安全加固 | seccomp RuntimeDefault、AppArmor、`allowPrivilegeEscalation: false`、只读根 |

### 必须先解决的工程问题

1. **日志与输出分离**：容器 stdout 是唯一结果通道时，stderr 需旁路（写 sidecar 或结构化单行 JSON，同阶段一）；
2. **Pod 泄漏**：控制器要有 finalizer + TTL 清理，否则失败 Pod 会长期占配额；
3. **镜像分发**：沙箱节点需要私有 registry 或镜像预热 DaemonSet；
4. **配额与公平**：namespace 级配额是粗粒度，租户多时要做层级配额（ResourceQuota + 准入控制）；
5. **可观测性**：按 Pod 标签聚合执行时长、失败率、逃逸告警。

### 验收标准（进入阶段三前必须全部满足）

- 单节点故障时，在途执行被标记为 `EXECUTION_UNKNOWN`，不自动重发；
- 跨租户读执行记录仍返回 404；
- 冷启动 P95 < 2 s，执行成功率 > 99%（不含用户代码自身失败）；
- 无 Pod 泄漏（连续 24h 后 `kubectl get pods` 与记录数一致）；
- 平台侧代码零改动，只改 `SANDBOX_BASE_URL`。

## 3. 阶段三：Firecracker Worker Pool（存档，不实现）

### 目标

在自有裸金属/VM 池上跑大量 microVM，**每次执行一个 VM**，用快照恢复把启动
压到 100–300ms；隔离强于 Kata（更小的 VMM 攻击面），密度高于传统 VM。

### 参考结构

```
平台 ──HTTP──▶ 沙箱网关（无状态）
                  │ 租户 / 配额 / 记录（PG）
                  ▼
              调度器（bin-pack，按租户打散）
                  ▼
        ┌─────────┬─────────┬─────────┐
        │ Worker1 │ Worker2 │ Worker3 │  ← 裸金属，每台数十到数百 microVM
        │ jailer  │ jailer  │ jailer  │
        │ VM×N    │ VM×N    │ VM×N    │
        └─────────┴─────────┴─────────┘
            每次执行：恢复快照 → vsock 传代码 → 收集结果 → 销毁 VM
```

### 设计要点

| 项 | 做法 |
|---|---|
| 启动 | 预启动 VM + 快照（snapshot/restore），恢复后由 vsock agent 接收代码 |
| 隔离 | 每 VM 独立内核 + jailer（chroot/namespace/cgroup）+ 独立网络命名空间（默认无网） |
| 文件系统 | 只读 rootfs（共享、按内容寻址）+ 每 VM 独立可写层，销毁即丢 |
| 通信 | vsock，不经 TCP；宿主侧 agent 只接受长度前缀的 JSON |
| 调度 | 按租户打散到不同物理机，避免同租户集中在单点；bin-pack 提升密度 |
| 生命周期 | 执行超时 → 直接 destroy VM（不依赖 guest 内进程自杀） |
| 观测 | 每 VM 的启动耗时、执行耗时、销毁结果；宿主级 cgroup 计量 |

### 必须先解决的工程问题

1. **快照与内核版本绑定**：升级 guest 内核要重建快照，需要版本化与灰度；
2. **存储与镜像**：rootfs 需要内容寻址 + 本地缓存，否则每次恢复都在拉镜像；
3. **网络策略**：默认无网，但"需要出网"的合法场景要有受控 egress（NAT + 白名单）；
4. **故障回收**：宿主崩溃后 VM 与 tap 设备、cgroup 都要能清理；
5. **安全审计**：jailer 配置、seccomp、KVM 权限、宿主内核补丁节奏。

### 不建议现在做的理由

- 阶段一的隔离已能挡住"误操作 + 一般恶意代码"，真正的强隔离需求尚未出现；
- 自研调度器与快照管理是持续投入，团队规模不够时会把维护成本变成主要风险；
- 阶段二已经能用托管/半托管方式拿到"每租户独立内核"，投入产出比更高。

## 4. 迁移纪律（三阶段通用）

1. **协议是契约**：新增能力用 `/v1/health` 的 `features` 声明，不改既有字段语义；
2. **状态外置**：执行记录一旦要跨副本，就必须进 PG/Redis，不能留在网关内存；
3. **失败语义不变**：超时与不可达一律 `retryable=false`，宁可人工确认，不自动重发；
4. **先灰度一个租户**：新后端先接内部租户，观察一周再放量；
5. **回滚路径**：`SANDBOX_BASE_URL` 指回旧服务即可，平台侧无状态。

## 5. 非目标

- 不做交互式 Notebook / 长驻开发环境（生命周期与配额模型完全不同）；
- 不做 GPU 沙箱（需要设备直通与驱动隔离，另立方案）；
- 不承诺"绝对安全"：任何阶段都要假设隔离可能被突破，靠最小权限与审计降低影响面。
