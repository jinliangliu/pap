"""沙箱镜像的进程启动钩子：让删除走回收站。

放在这里的理由是**生效时机**：`sitecustomize` 由解释器在启动时自动导入，
比任何用户代码都早。模型写的那段脚本里 `import os; os.remove(...)` 不需要
知道这层存在——它拿到的是已经被换过的 `os.remove`。

开关是「装上了就是开着的」，因为**沙箱里没有可传的变量**：
`/opt/pap/runner.py` 起用户代码子进程时用的是一份写死的最小环境
（PATH / HOME / LANG / PYTHONIOENCODING / PYTHONDONTWRITEBYTECODE），
镜像的 `ENV` 传不到用户代码里。于是「必须显式打开」这条在这里等于
「永远不打开」——实测过，`os.remove` 上并没有被换掉。

所以镜像本身就是那个开关：文件在 site-packages 里 → 生效。
`PAP_SAFE_DELETE=0`（或 false/no/off）可以关掉，但那只在宿主侧或将来
runner 转发环境变量时才有意义，留着是为了本地调试这层钩子本身。

`PAP_SAFE_DELETE_MAX_ITEMS` 只影响整棵目录的上限（`shutil.rmtree` 走的那条），
默认 1000；单文件删除不受它影响。同上，沙箱内目前取默认值。

整个文件不允许抛异常：`sitecustomize` 报错会让**每一个** Python 进程
启动失败，包括本来跟删除无关的。所有失败都降级成「这层没生效」并写一行
警告到 stderr——沙箱里没人看日志，但至少不会因此起不来。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_OFF = {"0", "false", "no", "off"}


def _load_module():
    """优先从已安装的 `app` 包导入；镜像里只拷了本文件时从同目录加载。

    两条路径都保留，是因为沙箱镜像的两种构建方式都存在：一种是把 PAP 的
    包一起装进去（开发镜像），一种是只 COPY 这个目录（最小镜像）。
    """
    try:
        from app.sandbox_guard import safe_delete  # noqa: PLC0415

        return safe_delete
    except Exception:
        pass

    candidate = Path(__file__).with_name("safe_delete.py")
    if not candidate.exists():
        return None

    spec = importlib.util.spec_from_file_location("pap_safe_delete", candidate)
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install() -> None:
    # 默认开，只有写了明确的「关」才不装。原因见文件头：沙箱里环境变量
    # 到不了这里，把「没写」当成「不开」就等于这层永远不生效。
    if os.environ.get("PAP_SAFE_DELETE", "").strip().lower() in _OFF:
        return

    module = _load_module()
    if module is None:
        print(
            "PAP_SAFE_DELETE 已打开，但找不到 safe_delete 模块，删除护栏未生效。",
            file=sys.stderr,
        )
        return

    # 上限按镜像规格可调，但**读坏了就用默认**：一个写错的数字不该让
    # 整个护栏降级成原生删除，那是最坏的一种失败方向。
    max_items = module.MAX_TREE_ITEMS
    raw = os.environ.get("PAP_SAFE_DELETE_MAX_ITEMS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        max_items = int(raw)

    try:
        module.install(max_items=max_items)
    except Exception as exc:  # noqa: BLE001
        print(f"删除护栏安装失败，本次按原生删除运行：{exc}", file=sys.stderr)


_install()
