# app/observability/metrics.py
"""最小可用指标：请求数、错误数、耗时、工具调用与模型调用计数。

以 Prometheus 文本格式暴露在 /metrics，足以支撑开发与单机部署的观测需求。
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Final

_lock = Lock()

_counters: dict[str, int] = defaultdict(int)
_duration_sum: dict[str, float] = defaultdict(float)
_duration_count: dict[str, int] = defaultdict(int)

HELP_TEXT: Final[dict[str, str]] = {
    "http_requests_total": "HTTP 请求总数",
    "http_errors_total": "HTTP 5xx 响应总数",
    "tool_calls_total": "工具调用总数",
    "tool_failures_total": "工具调用失败总数",
    "model_calls_total": "模型调用总数",
    "model_failures_total": "模型调用失败总数",
    "run_status_total": "运行状态分布",
}


def inc(name: str, *, labels: str = "", value: int = 1) -> None:
    key = f"{name}{labels}"
    with _lock:
        _counters[key] += value


def observe(name: str, duration_ms: float, *, labels: str = "") -> None:
    key = f"{name}{labels}"
    with _lock:
        _duration_sum[key] += duration_ms
        _duration_count[key] += 1


def render() -> str:
    lines: list[str] = []

    with _lock:
        counters = dict(_counters)
        sums = dict(_duration_sum)
        counts = dict(_duration_count)

    for name in sorted(HELP_TEXT):
        lines.append(f"# HELP {name} {HELP_TEXT[name]}")
        lines.append(f"# TYPE {name} counter")
        for key, value in counters.items():
            if key.startswith(name):
                lines.append(f"{key} {value}")

    lines.append("# TYPE http_request_duration_ms_avg gauge")
    for key, total in sums.items():
        count = counts.get(key, 0)
        if count:
            lines.append(f"{key} {total / count:.2f}")

    return "\n".join(lines) + "\n"


def reset() -> None:
    with _lock:
        _counters.clear()
        _duration_sum.clear()
        _duration_count.clear()
