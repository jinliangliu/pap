# tests/unit/test_import_order.py
"""包能不能被单独导入，不该取决于谁先来。

`app.knowledge.service` 与 `app.agents` 之间是双向依赖：前者在顶层 import
`app.agents.prompts`，而 `app.agents` → `graph` → `nodes` 又 import 回
`app.knowledge.service`。应用启动时先碰 agents，所以线上一直是好的；任何脚本
或用例先 import `app.tools.builtins` 就直接 `partially initialized module`。

这类问题在同一个进程里测不出来（模块已经导入完成），只能开一个新解释器。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _import_first(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_tools_package_can_be_imported_first():
    done = _import_first("app.tools.builtins")

    assert done.returncode == 0, done.stderr[-2000:]


def test_knowledge_service_can_be_imported_first():
    done = _import_first("app.knowledge.service")

    assert done.returncode == 0, done.stderr[-2000:]
