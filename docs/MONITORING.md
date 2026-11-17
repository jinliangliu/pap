# 监控与告警

服务端每 1 分钟跑一次 `scripts/watchdog.py`，检查 7 项，异常时推送到群机器人
或自定义命令。没有引入 Prometheus / Grafana——这台机器是单实例，装一整套
监控栈的运维成本高于它解决的问题。等出现多实例或需要看趋势时再说，
巡检脚本的 JSON 输出已经可以喂给采集器。

## 1. 检查项

| 检查 | 判据 | 失败意味着 |
|---|---|---|
| `service` | `systemctl is-active personal-agent-platform` | 进程没了。先看 `journalctl -u personal-agent-platform -n 80` |
| `ready` | `GET /ready` 返回 200 且 `status=ok` | 依赖有问题。detail 里会列出具体失败项（`database` / `checkpointer` / `tool_registry` / `rate_limiter`） |
| `database` | 容器 `personal-agent-postgres` 状态为 `running` | 库挂了或容器在重启循环 |
| `mcp` | TCP 能连上 `127.0.0.1:8765` | 远端调压 MCP 服务不可达，`prv_*` 工具会全部失效；对话、知识检索仍可用 |
| `disk` | 挂载点使用率低于 85% | 日志、导出文件、容器层在涨。低于 5% 会直接写不进库 |
| `memory` | `MemAvailable` 高于 512 MiB | 内存被占满，随后会出现 OOM kill |
| `http_5xx` | nginx 近 1000 条请求里 5xx 不超过 20 条 | 上游在报错或者超时。**不一定是应用的问题**，也可能是客户端在打错接口 |

`ready` 与 `database` 有重叠但不冗余：`/ready` 走的是应用自己的连接池，
容器检查走的是 docker 状态。应用连不上库但容器还活着（连接数耗尽、权限变更）
只有前者能发现；应用本身挂了则两者都会红。

`mcp` 只做 TCP 连通，因为 `/ready` 的 `mcp_tools` 项只说明工具注册成功，
不代表远端活着——这是两个不同的故障面。

没有 systemctl / docker 的环境（比如开发机）对应项会显示「跳过」而不是失败，
所以在 Windows 上跑也不会满屏红。

## 2. 安装

跟着 `deploy/install.sh` 走，默认就会装：

```bash
PAP_USER=pap-admin PAP_PASSWORD='<强密码>' bash deploy/install.sh
```

它做四件事：装 `pap-watchdog.service` / `.timer`、建 `/var/lib/pap-watchdog`、
在缺失时生成 `/etc/pap-watchdog.env`、`enable --now` 定时器并立刻首跑一次。
`PAP_INSTALL_WATCHDOG=0` 可跳过。

只要单元文件、不想让脚本动定时器时：

```bash
install -m 0644 deploy/systemd/pap-watchdog.service /etc/systemd/system/
install -m 0644 deploy/systemd/pap-watchdog.timer   /etc/systemd/system/
install -d -m 0750 /var/lib/pap-watchdog
systemctl daemon-reload && systemctl enable --now pap-watchdog.timer
```

## 3. 配置告警通道

编辑 `/etc/pap-watchdog.env`（模板见 `deploy/pap-watchdog.env.example`）。
**改完不用重启**，下一分钟的巡检就读到新值。

群机器人：

```dotenv
WATCHDOG_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
WATCHDOG_WEBHOOK_TYPE=wecom
```

`WATCHDOG_WEBHOOK_TYPE` 支持 `wecom`（企业微信）、`dingtalk`（钉钉）、
`feishu`（飞书）、`slack`、`generic`（POST `{"text": "..."}`）。填错会自动
退到 `generic`——告警宁可格式不对，也不能发不出去。

自定义命令，正文从 stdin 传入：

```dotenv
WATCHDOG_ALERT_CMD=mail -s "PAP 告警" ops@example.com
```

**一个通道都不配也能跑**：告警会写进 journal。这不是好方案，但比没有强，
而且保证巡检不会因为「没配通道」而崩溃。

验证通道：

```bash
python3 /personal-agent-platform/scripts/watchdog.py --test-alert
```

## 4. 告警节奏

```
第 1 次失败  → 不报（记录 fails=1）
第 2 次失败  → 报「故障」，记下报警时刻
之后每 30 分钟 → 报一次「持续未恢复」
恢复正常     → 报一次「恢复」，并清空计数
```

规则都在 `evaluate()` 里，是纯函数，时间由调用方传入，测试见
`tests/unit/test_watchdog.py`。

为什么不是「失败就报」：服务重启、数据库抖动、机器负载尖峰都会造出单次失败。
立刻报警的结果是值班的人三天内就学会忽略这个群。

为什么恢复也要报：只报坏了不报好了，收到告警的人不知道该不该继续盯着。

`WATCHDOG_FAIL_THRESHOLD=1` 可改成立刻报，代价是每次 `systemctl restart`
都会响一次。`WATCHDOG_REPEAT_SECONDS` 控制重复提醒间隔。

## 5. 日常查看

```bash
# 最近几次巡检结论（每行一条 JSON）
journalctl -u pap-watchdog --no-pager | grep watchdog_checks | tail -5

# 发出去的告警原文
journalctl -u pap-watchdog --no-pager | grep watchdog_alert | tail -3

# 手工跑一次，看完整明细
python3 /personal-agent-platform/scripts/watchdog.py

# 只看结果，不发告警，有失败时退出码为 1（适合接外部定时任务）
python3 /personal-agent-platform/scripts/watchdog.py --no-alert --strict

# 机器可读
python3 /personal-agent-platform/scripts/watchdog.py --json
```

状态文件 `/var/lib/pap-watchdog/state.json` 存的是每项检查的连续失败数与
报警时刻。删掉它只会让下次故障重新计数，不会丢失任何历史告警。

## 6. 局限

- **抓不到「吞掉异常仍回 200」**。`http_5xx` 只看状态码，应用把错误包装成
  200 时它不会红。这类问题要看应用日志里的 `event=...` 结构化字段。
- **没有历史趋势**。只有当前状态 + 最近一次事件，画不出「磁盘这周涨了多少」。
  需要时把 `--json` 的输出定期落到文件，或接 Prometheus textfile collector。
- **单机视角**。所有探测都是本机回环，外网可达性（DNS、证书、防火墙）不在
  范围内。若以后加了 TLS 与域名，应额外补一条外部探测。
- **告警发送失败只记日志**。webhook 挂了不会重试，只会写
  `watchdog_webhook_failed`。以 1 分钟的巡检频率，下一次失败窗口会自然重发。

## 7. 与业务侧的关系

巡检不碰业务数据、不用 `.env`（配置独立在 `/etc/pap-watchdog.env`），
所以它拿不到模型密钥和数据库口令。以 root 运行只是为了读 docker socket
和 nginx 日志（`adm` 组），单元里用 `ProtectSystem=full` 把 `/usr`、`/etc`
挂成只读。
