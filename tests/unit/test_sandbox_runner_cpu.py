# tests/unit/test_sandbox_runner_cpu.py
"""容器内 runner 的 CPU 计量。

`deploy/sandbox/image/` 刻意不是包（镜像只 COPY 几个文件），所以按路径加载。
测的是计量语义而不是 getrusage 本身：
- 差值来自 RUSAGE_CHILDREN，不是墙钟；
- 计数器回退或采集失败时返回 None，绝不返回 0 或负数。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# resource 是 POSIX-only，Windows 开发机上不存在；runner 只在 Linux 容器里跑。
# 这里注入一个最小假模块，让我们能测计量逻辑本身（而不是资源限制）。
if "resource" not in sys.modules:
    try:
        import resource  # noqa: F401
    except ImportError:
        fake = types.ModuleType("resource")
        fake.RUSAGE_CHILDREN = 1  # type: ignore[attr-defined]
        fake.RLIMIT_CPU = 0  # type: ignore[attr-defined]
        fake.RLIMIT_FSIZE = 1  # type: ignore[attr-defined]
        fake.RLIMIT_NPROC = 2  # type: ignore[attr-defined]
        fake.RLIMIT_NOFILE = 3  # type: ignore[attr-defined]
        fake.RLIMIT_CORE = 4  # type: ignore[attr-defined]
        fake.getrusage = lambda _who: types.SimpleNamespace(  # type: ignore[attr-defined]
            ru_utime=0.0, ru_stime=0.0
        )
        fake.setrlimit = lambda *_args: None  # type: ignore[attr-defined]
        sys.modules["resource"] = fake

_RUNNER = Path(__file__).resolve().parents[2] / "deploy" / "sandbox" / "image" / "runner.py"
_spec = importlib.util.spec_from_file_location("pap_sandbox_runner", _RUNNER)
assert _spec is not None and _spec.loader is not None  # noqa: S101
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_child_cpu_ms_returns_nonnegative_int_or_none():
    value = runner._child_cpu_ms()
    assert value is None or (isinstance(value, int) and value >= 0)


def test_cpu_ms_since_returns_difference(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "_child_cpu_ms", lambda: 150)
    assert runner._cpu_ms_since(112) == 38


def test_cpu_ms_since_none_before_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "_child_cpu_ms", lambda: 150)
    # 起始值没测到：不能用一个后续值冒充「这段代码消耗了 150ms」。
    assert runner._cpu_ms_since(None) is None


def test_cpu_ms_since_counter_rollback_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "_child_cpu_ms", lambda: 7)
    # 计数器回退（异常实现/复用）：报「没测到」而不是负数。
    assert runner._cpu_ms_since(42) is None


def test_cpu_ms_since_unavailable_now_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "_child_cpu_ms", lambda: None)
    assert runner._cpu_ms_since(42) is None


def test_zero_is_distinct_from_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "_child_cpu_ms", lambda: 42)
    # 代码跑完但没消耗 CPU：0 是有效读数，不能被当成缺失。
    assert runner._cpu_ms_since(42) == 0
