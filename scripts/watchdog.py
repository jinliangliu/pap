#!/usr/bin/env python3
"""服务端巡检与告警。

一次运行 = 采集检查项 → 与上次状态比对 → 该报警时推一条消息。
由 deploy/systemd/pap-watchdog.timer 每分钟触发。

设计取舍：
- **只用标准库**。巡检脚本是「服务挂了之后唯一还活着的东西」，
  它不能因为依赖装错、虚拟环境损坏而一起失效。
- **连续失败才报警**（默认 2 次）。服务重启、数据库瞬时抖动都会造出
  一次失败；立刻报警会把人训练成忽略告警。
- **恢复也要报**。只报「坏了」不报「好了」，值班的人不知道该不该收工。
- **状态落文件**。进程每次都是新的，去重窗口只能靠状态文件维持；
  没有状态文件时退化为每次都报（宁可吵，不要漏）。

用法：
    python3 scripts/watchdog.py                # 单次巡检
    python3 scripts/watchdog.py --json         # 输出机器可读结果
    python3 scripts/watchdog.py --test-alert   # 只发一条测试告警
    python3 scripts/watchdog.py --strict       # 有失败时退出码 1（给人工用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = "/var/lib/pap-watchdog/state.json"

# nginx combined 日志里状态码跟在带引号的请求行后面。
_STATUS_RE = re.compile(r'"\s+(\d{3})\s')


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True, slots=True)
class Config:
    base_url: str = "http://127.0.0.1:8000"
    service: str = "personal-agent-platform.service"
    db_container: str = "personal-agent-postgres"
    mcp_addr: str = "127.0.0.1:8765"
    disk_path: str = "/"
    disk_max_percent: float = 85.0
    mem_min_available_mb: int = 512
    access_log: str = "/var/log/nginx/access.log"
    access_log_lines: int = 1000
    http_5xx_max: int = 20
    fail_threshold: int = 2
    repeat_seconds: int = 1800
    timeout: float = 8.0
    state_file: str = DEFAULT_STATE_FILE
    webhook_url: str = ""
    webhook_type: str = "generic"
    alert_cmd: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        # slots=True 的 dataclass 上，类属性是 member descriptor 而不是默认值，
        # 所以默认值只能从实例上取。
        defaults = cls()

        def s(key: str, default: str) -> str:
            value = (e.get(key) or "").strip()
            return value or default

        def f(key: str, default: float) -> float:
            try:
                return float(s(key, str(default)))
            except ValueError:
                return default

        def i(key: str, default: int) -> int:
            try:
                return int(float(s(key, str(default))))
            except ValueError:
                return default

        return cls(
            base_url=s("WATCHDOG_BASE_URL", defaults.base_url),
            service=s("WATCHDOG_SERVICE", defaults.service),
            db_container=s("WATCHDOG_DB_CONTAINER", defaults.db_container),
            mcp_addr=s("WATCHDOG_MCP_ADDR", defaults.mcp_addr),
            disk_path=s("WATCHDOG_DISK_PATH", defaults.disk_path),
            disk_max_percent=f("WATCHDOG_DISK_MAX_PERCENT", defaults.disk_max_percent),
            mem_min_available_mb=i("WATCHDOG_MEM_MIN_AVAILABLE_MB", defaults.mem_min_available_mb),
            access_log=s("WATCHDOG_ACCESS_LOG", defaults.access_log),
            access_log_lines=i("WATCHDOG_ACCESS_LOG_LINES", defaults.access_log_lines),
            http_5xx_max=i("WATCHDOG_HTTP_5XX_MAX", defaults.http_5xx_max),
            fail_threshold=max(1, i("WATCHDOG_FAIL_THRESHOLD", defaults.fail_threshold)),
            repeat_seconds=i("WATCHDOG_REPEAT_SECONDS", defaults.repeat_seconds),
            timeout=f("WATCHDOG_TIMEOUT_SECONDS", defaults.timeout),
            state_file=s("WATCHDOG_STATE_FILE", defaults.state_file),
            webhook_url=s("WATCHDOG_WEBHOOK_URL", ""),
            webhook_type=s("WATCHDOG_WEBHOOK_TYPE", defaults.webhook_type),
            alert_cmd=s("WATCHDOG_ALERT_CMD", ""),
        )._normalized()

    def _normalized(self) -> Config:
        # 地址写错的代价是「静默不报警」，比崩溃更糟，这里统一兜住格式。
        addr = self.mcp_addr if ":" in self.mcp_addr else self.mcp_addr + ":8765"
        webhook_type = self.webhook_type.strip().lower()
        if webhook_type not in {"generic", "wecom", "dingtalk", "feishu", "slack"}:
            webhook_type = "generic"
        return replace(self, mcp_addr=addr, webhook_type=webhook_type)


# ---------------------------------------------------------------- 检查项


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _run(args: list[str], timeout: float) -> tuple[int | None, str]:
    """跑一条命令。命令不存在时返回 (None, "")，调用方据此跳过检查。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, ""
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{args[0]}）"
    except OSError as exc:
        return None, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    # 本机探测走代理会绕到外面，既慢又会伪装成「服务正常」。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check_service(cfg: Config) -> CheckResult:
    code, out = _run(["systemctl", "is-active", cfg.service], cfg.timeout)
    if code is None and not out:
        return CheckResult("service", True, "跳过：本机没有 systemctl")
    state = out.splitlines()[0].strip() if out else "unknown"
    return CheckResult("service", state == "active", f"{cfg.service} 状态 {state}")


def check_ready(cfg: Config) -> CheckResult:
    url = cfg.base_url.rstrip("/") + "/ready"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with _no_proxy_opener().open(request, timeout=cfg.timeout) as resp:
            code = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 503 的响应体里有具体失败项，正是最需要的内容。
        code = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return CheckResult("ready", False, f"{url} 不可达：{reason}")

    try:
        body = json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return CheckResult("ready", False, f"{url} 返回非 JSON（HTTP {code}）")

    failing = [name for name, ok in (body.get("checks") or {}).items() if ok is False]
    status = body.get("status") or "unknown"
    ok = code == 200 and status == "ok"

    if ok:
        return CheckResult("ready", True, f"就绪，http={code}")

    detail = f"status={status} http={code}"
    if failing:
        detail += f"，失败项：{', '.join(failing)}"
    return CheckResult("ready", False, detail)


def check_database(cfg: Config) -> CheckResult:
    code, out = _run(
        ["docker", "inspect", "-f", "{{.State.Status}}", cfg.db_container],
        cfg.timeout,
    )
    if code is None and not out:
        return CheckResult("database", True, "跳过：本机没有 docker")
    state = out.splitlines()[0].strip() if out else "unknown"
    return CheckResult("database", state == "running", f"容器 {cfg.db_container} 状态 {state}")


def check_mcp(cfg: Config) -> CheckResult:
    """只做 TCP 连通。MCP 是否「能用」由 /ready 的 mcp_tools 项负责。"""
    host, _, port = cfg.mcp_addr.rpartition(":")
    if not host or not port.isdigit():
        return CheckResult("mcp", True, f"跳过：地址格式不支持（{cfg.mcp_addr}）")

    try:
        with socket.create_connection((host, int(port)), timeout=cfg.timeout):
            pass
    except OSError as exc:
        return CheckResult("mcp", False, f"TCP {cfg.mcp_addr} 连不上：{exc}")
    return CheckResult("mcp", True, f"TCP {cfg.mcp_addr} 可连接")


def check_disk(cfg: Config) -> CheckResult:
    try:
        usage = shutil.disk_usage(cfg.disk_path)
    except OSError as exc:
        return CheckResult("disk", True, f"跳过：{cfg.disk_path} 不可读（{exc}）")

    percent = usage.used / usage.total * 100 if usage.total else 0.0
    ok = percent < cfg.disk_max_percent
    return CheckResult(
        "disk",
        ok,
        f"{cfg.disk_path} 已用 {percent:.0f}%（阈值 {cfg.disk_max_percent:.0f}%），"
        f"剩余 {usage.free / 1024**3:.1f} GiB",
    )


def check_memory(cfg: Config) -> CheckResult:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return CheckResult("memory", True, "跳过：无 /proc/meminfo")

    try:
        text = meminfo.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("memory", True, f"跳过：读取失败（{exc}）")

    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    if not match:
        return CheckResult("memory", True, "跳过：meminfo 里没有 MemAvailable")

    available_mb = int(match.group(1)) / 1024
    ok = available_mb >= cfg.mem_min_available_mb
    return CheckResult(
        "memory",
        ok,
        f"可用 {available_mb:.0f} MiB（下限 {cfg.mem_min_available_mb} MiB）",
    )


def check_http_5xx(cfg: Config) -> CheckResult:
    """
    近 N 行请求里的 5xx 次数。

    这是粗糙但有效的信号：应用把异常吞掉、仍回 200 的情况它抓不到，
    但那类问题在日志里另有线索，而「一片 502」正是最需要立刻知道的事。
    """
    log = Path(cfg.access_log)
    if not log.exists():
        return CheckResult("http_5xx", True, f"跳过：{cfg.access_log} 不存在")

    try:
        with log.open("rb") as handle:
            # 只读尾部：access.log 可能上百 MB，整读会把巡检拖慢。
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            tail = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return CheckResult("http_5xx", True, f"跳过：无法读取（{exc}）")

    lines = tail.splitlines()[-cfg.access_log_lines :]
    errors = 0
    total = 0
    for line in lines:
        match = _STATUS_RE.search(line)
        if not match:
            continue
        total += 1
        if int(match.group(1)) >= 500:
            errors += 1

    ok = errors <= cfg.http_5xx_max
    return CheckResult(
        "http_5xx",
        ok,
        f"近 {total} 条请求中 {errors} 条 5xx（阈值 {cfg.http_5xx_max}）",
    )


CHECKS = (
    check_service,
    check_ready,
    check_database,
    check_mcp,
    check_disk,
    check_memory,
    check_http_5xx,
)


def run_checks(cfg: Config) -> list[CheckResult]:
    results = []
    for check in CHECKS:
        try:
            results.append(check(cfg))
        except Exception as exc:  # 单个检查项崩了不能拖垮整轮巡检
            results.append(
                CheckResult(check.__name__.removeprefix("check_"), False, f"检查异常：{exc}")
            )
    return results


# ---------------------------------------------------------------- 告警判定


@dataclass(frozen=True, slots=True)
class Event:
    kind: str  # firing / recovered
    name: str
    detail: str
    failing_since: float | None = None
    is_repeat: bool = False


def evaluate(
    results: Iterable[CheckResult],
    state: dict[str, Any],
    *,
    now: float,
    fail_threshold: int,
    repeat_seconds: int,
) -> tuple[dict[str, Any], list[Event]]:
    """
    比对上一轮状态，决定这一轮要不要发告警。

    纯函数：时间由调用方传入，测试可以精确控制「持续了多久」。
    """
    previous = state.get("checks") or {}
    events: list[Event] = []
    current: dict[str, Any] = {}

    for result in results:
        prev = previous.get(result.name) or {}
        entry = {
            "fails": int(prev.get("fails") or 0),
            "failing_since": prev.get("failing_since"),
            "last_alert_at": prev.get("last_alert_at"),
        }

        if result.ok:
            # 之前报过故障才发恢复通知；一直正常的不必天天报平安。
            if entry["last_alert_at"] is not None:
                events.append(
                    Event(
                        kind="recovered",
                        name=result.name,
                        detail=result.detail,
                        failing_since=entry["failing_since"],
                    )
                )
            current[result.name] = {"fails": 0, "failing_since": None, "last_alert_at": None}
            continue

        entry["fails"] += 1
        if entry["failing_since"] is None:
            entry["failing_since"] = now

        last_alert = entry["last_alert_at"]
        due = last_alert is None or (now - float(last_alert)) >= repeat_seconds

        if entry["fails"] >= fail_threshold and due:
            events.append(
                Event(
                    kind="firing",
                    name=result.name,
                    detail=result.detail,
                    failing_since=entry["failing_since"],
                    is_repeat=last_alert is not None,
                )
            )
            entry["last_alert_at"] = now

        current[result.name] = entry

    return {"checks": current}, events


def _duration(seconds: float | None) -> str:
    """
    把秒数说成人话。

    1 分钟以内统一说「不足 1 分钟」：告警刚触发时两个时间戳常常落在同一秒，
    显示「0 秒」「刚刚」既没信息量，接在「已持续」后面也不像中文。
    """
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "不足 1 分钟"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时{(seconds % 3600) // 60} 分钟"
    return f"{seconds // 86400} 天"


def format_message(events: list[Event], *, now: float, host: str) -> str:
    stamp = datetime.fromtimestamp(now, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    firing = [e for e in events if e.kind == "firing"]
    recovered = [e for e in events if e.kind == "recovered"]

    if firing:
        head = f"[PAP 告警] {host} {len(firing)} 项检查异常"
    elif recovered:
        head = f"[PAP 恢复] {host} {len(recovered)} 项检查已恢复"
    else:
        head = f"[PAP 巡检] {host}"

    lines = [head, stamp, ""]

    for event in firing:
        suffix = "（持续未恢复）" if event.is_repeat else ""
        lines.append(f"✗ {event.name}：{event.detail}{suffix}")
        lines.append(f"  已持续{_duration(now - event.failing_since)}")
    for event in recovered:
        lines.append(f"✓ {event.name}：{event.detail}")
        if event.failing_since:
            lines.append(f"  故障持续{_duration(now - event.failing_since)}")

    if firing:
        lines.append("")
        lines.append("排查：journalctl -u personal-agent-platform -n 80 --no-pager")

    return "\n".join(lines)


# ---------------------------------------------------------------- 发送

_WEBHOOK_BODIES = {
    "wecom": lambda text: {"msgtype": "text", "text": {"content": text}},
    "dingtalk": lambda text: {"msgtype": "text", "text": {"content": text}},
    "feishu": lambda text: {"msg_type": "text", "content": {"text": text}},
    "slack": lambda text: {"text": text},
}


def _log(event: str, **fields: Any) -> None:
    payload = {"event": event, "ts": datetime.now(UTC).isoformat(), **fields}
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")


def send_webhook(cfg: Config, text: str) -> bool:
    if not cfg.webhook_url:
        return False

    builder = _WEBHOOK_BODIES.get(cfg.webhook_type, lambda t: {"text": t})
    data = json.dumps(builder(text), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        cfg.webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with _no_proxy_opener().open(request, timeout=cfg.timeout) as resp:
            if 200 <= resp.status < 300:
                return True
            _log("watchdog_webhook_failed", status=resp.status)
    except Exception as exc:
        _log("watchdog_webhook_failed", error=f"{type(exc).__name__}: {exc}")
    return False


def send_command(cfg: Config, text: str) -> bool:
    if not cfg.alert_cmd:
        return False
    try:
        proc = subprocess.run(
            cfg.alert_cmd,
            shell=True,
            input=text,
            text=True,
            capture_output=True,
            timeout=cfg.timeout * 3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log("watchdog_cmd_failed", error=str(exc))
        return False

    if proc.returncode != 0:
        _log("watchdog_cmd_failed", status=proc.returncode, stderr=(proc.stderr or "")[:300])
        return False
    return True


def dispatch(cfg: Config, text: str) -> None:
    """
    发出去。

    没有任何通道配置时只写 journal——这是最差但可用的兜底：
    `journalctl -u pap-watchdog` 里能查到，不影响巡检本身。
    """
    jobs = (send_webhook, send_command)
    sent = sum(1 for job in jobs if job(cfg, text))
    _log("watchdog_alert", delivered=sent, message=text)


# ---------------------------------------------------------------- 状态


def load_state(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"checks": {}}
    except OSError as exc:
        _log("watchdog_state_unreadable", path=path, error=str(exc))
        return {"checks": {}}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 文件损坏时按「没有历史」处理：宁可重复报一次，也不要静默。
        _log("watchdog_state_corrupt", path=path)
        return {"checks": {}}

    return data if isinstance(data, dict) else {"checks": {}}


def save_state(path: str, state: dict[str, Any]) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        # 存不下状态只会导致多报几次，不应让巡检失败。
        _log("watchdog_state_unwritable", path=path, error=str(exc))


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Agent Platform 服务端巡检")
    parser.add_argument("--json", action="store_true", help="输出机器可读的检查结果")
    parser.add_argument("--test-alert", action="store_true", help="只发一条测试告警")
    parser.add_argument("--strict", action="store_true", help="有失败项时退出码为 1")
    parser.add_argument("--no-alert", action="store_true", help="只检查不发告警（手工排查用）")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    now = datetime.now(UTC).timestamp()
    host = socket.gethostname()

    if args.test_alert:
        text = format_message(
            [Event(kind="firing", name="test", detail="这是一条测试告警，无需处理。")],
            now=now,
            host=host,
        )
        dispatch(cfg, text)
        return 0

    results = run_checks(cfg)
    state = load_state(cfg.state_file)
    new_state, events = evaluate(
        results,
        state,
        now=now,
        fail_threshold=cfg.fail_threshold,
        repeat_seconds=cfg.repeat_seconds,
    )

    failing = [r for r in results if not r.ok]

    if args.json:
        print(
            json.dumps(
                {
                    "host": host,
                    "timestamp": datetime.fromtimestamp(now, tz=UTC).isoformat(),
                    "ok": not failing,
                    "checks": [r.as_dict() for r in results],
                    "events": [e.__dict__ for e in events],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        status = "OK" if not failing else f"FAIL {len(failing)}"
        print(f"[{status}] 巡检 {len(results)} 项")
        for result in results:
            print(f"  {'ok  ' if result.ok else 'FAIL'} {result.name}: {result.detail}")
        for event in events:
            print(f"  → {event.kind} {event.name}")

    _log(
        "watchdog_checks",
        ok=not failing,
        total=len(results),
        failing=[r.name for r in failing],
    )

    if events and not args.no_alert:
        dispatch(cfg, format_message(events, now=now, host=host))

    save_state(cfg.state_file, new_state)

    if args.strict and failing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
