#!/usr/bin/env python3
"""容器内执行入口。

宿主侧通过 ``docker run`` 启动本文件，从 stdin 读 JSON，执行完把结果
以单行 JSON 写回 stdout。它只负责一段代码的生命周期——租户、审批、
配额、清理都在宿主侧，容器内一概不感知。

之所以不在宿主侧拼接命令行执行代码：那样代码会出现在 argv 里，
既受长度限制，也可能被 ``ps`` 看到（同宿主其它租户）。
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time

MAX_OUTPUT_CHARS = 200_000

LANGUAGES: dict[str, list[str]] = {
    "python": ["python3", "-I", "-B"],
    "javascript": ["node", "--no-warnings"],
}

ENTRYPOINTS = {"python": "main.py", "javascript": "main.js"}

WORKDIR = "/work"


def _limits(cpu_seconds: int, pids: int, nofile: int) -> None:  # pragma: no cover
    """容器内的第二层防线。

    cgroup 已经限制了内存与 CPU 总量，这里再补文件大小、进程数与句柄数，
    防止单个进程把 tmpfs 写满或 fork 出大量子进程。
    刻意不设 RLIMIT_AS：Node 的 V8 会预留大段虚拟地址空间，
    设了它 javascript 直接起不来；内存上限交给 cgroup。
    """
    os.setsid()

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NPROC, (pids, pids))
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _child_cpu_ms() -> int | None:
    """已退出的直接子进程累计 CPU 时间（毫秒）；不可用时返回 None。"""
    try:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return int((usage.ru_utime + usage.ru_stime) * 1000)
    except (AttributeError, OSError, ValueError):
        return None


def _cpu_ms_since(before: int | None) -> int | None:
    """ CPU 时间差。before 或当前值拿不到就返回 None，不用 0 冒充。"""
    if before is None:
        return None
    now = _child_cpu_ms()
    if now is None or now < before:
        # 跨进程复用 / 计数器回退：宁可报「没测到」也不报一个负数或假值。
        return None
    return now - before


def _run(payload: dict) -> dict:
    language = str(payload.get("language") or "")
    command = LANGUAGES.get(language)

    if command is None:
        return {
            "status": "failed",
            "exit_code": None,
            "stdout": "",
            "stderr": f"不支持的语言：{language}",
            "duration_ms": 0,
            "cpu_ms": None,
        }

    timeout_seconds = int(payload.get("timeout_seconds") or 30)
    entry = ENTRYPOINTS[language]
    code = str(payload.get("code") or "")
    stdin = str(payload.get("stdin") or "")

    os.makedirs(WORKDIR, exist_ok=True)

    with open(os.path.join(WORKDIR, entry), "w", encoding="utf-8") as handle:
        handle.write(code)

    started = time.monotonic()
    # CPU 时间取「代码子进程」的 user+sys 差值，不读 cgroup 路径：
    # cgroup v1/v2、systemd driver、云宿主的路径都不同，硬编码必错；
    # 而 getrusage(RUSAGE_CHILDREN) 在容器内稳定可得，语义也更准——
    # 只算这段代码真正消耗的 CPU，不含 runner 自身与容器启动开销。
    # 拿不到（平台实现差异）时留 None，绝不填 0：0 表示「测到了且没消耗」。
    cpu_before = _child_cpu_ms()

    try:
        completed = subprocess.run(  # noqa: S603
            [*command, entry],
            cwd=WORKDIR,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": WORKDIR,
                "LANG": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            preexec_fn=lambda: _limits(timeout_seconds, 64, 128),  # noqa: PLW1509
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": "",
            "stderr": f"执行超过 {timeout_seconds} 秒，已被终止。",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "cpu_ms": _cpu_ms_since(cpu_before),
        }

    return {
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:MAX_OUTPUT_CHARS],
        "stderr": (completed.stderr or "")[:MAX_OUTPUT_CHARS],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cpu_ms": _cpu_ms_since(cpu_before),
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        json.dump(
            {
                "status": "failed",
                "exit_code": None,
                "stdout": "",
                "stderr": "请求体不是合法 JSON。",
            },
            sys.stdout,
        )
        return 2

    if not isinstance(payload, dict):
        json.dump(
            {"status": "failed", "exit_code": None, "stdout": "", "stderr": "请求体结构错误。"},
            sys.stdout,
        )
        return 2

    json.dump(_run(payload), sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
