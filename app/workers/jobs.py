# app/workers/jobs.py
"""维护任务的独立入口。

适合用 cron / systemd timer 拉起的部署方式：
不想让 Web 进程带后台循环，或想单独控制维护节奏时用这个入口。

    uv run python -m app.workers.jobs

多实例下仍然受 advisory lock 保护，同一时刻只有一个实例真正执行。
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.workers.maintenance import run_maintenance_once


async def main() -> int:
    configure_logging(settings)

    try:
        await run_maintenance_once()
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
