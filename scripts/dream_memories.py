# scripts/dream_memories.py
"""离线跑一次梦境：核对自动抽取的记忆（dreaming 的最小实现）。

它解决的问题：自动抽取只看「那一轮」，写错了、用户后来改口了、
同一件事攒成两条，都发现不了。本脚本用该用户的会话原文做 ground truth，
产出「纠错 / 合并 / 下架」建议，**人工确认后**才生效。

用法：
    uv run python -m scripts.dream_memories --user alex
    uv run python -m scripts.dream_memories --user alex --dry-run
    uv run python -m scripts.dream_memories --user alex --days 60 --limit 30
    uv run python -m scripts.dream_memories --user alex --json /tmp/dream.json

边界：
- 只碰 `source=auto_extract` 且 `is_user_confirmed=false` 的候选；
- 只读会话里的 **user 消息**，助手自己的话不算证据；
- 模型未配置 / 单条失败只跳过该条，不会中断整批。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import settings
from app.db.session import async_session_factory, dispose_engine
from app.services import memory_dream
from app.tenancy.context import tenant_scope


async def _resolve_config(tenant: str) -> memory_dream.DreamConfig:
    with tenant_scope(tenant):
        async with async_session_factory() as session:
            return await memory_dream.resolve_dream_config(session, tenant_id=tenant)


async def _run(args: argparse.Namespace) -> int:
    tenant = args.tenant or settings.default_tenant_id

    # 与界面共用同一份配置：命令行和页面各按各的默认值跑，
    # 会让人以为「配了没生效」。
    config = await _resolve_config(tenant)
    if not config.enabled:
        print(f"梦境已关闭：{config.disabled_reason}", file=sys.stderr)
        return 2

    days = args.days if args.days is not None else config.days
    limit = config.clamp_limit(args.limit if args.limit is not None else config.max_per_run)
    from_config = args.days is None

    print(
        f"租户：{tenant} | 用户：{args.user} | 证据窗口：{days} 天"
        f"{'（租户配置）' if from_config else ''} | 上限：{limit}"
    )
    if args.dry_run:
        print("dry-run：只打印建议，不写库。")

    report = await memory_dream.run_dreaming(
        tenant_id=tenant,
        user_id=args.user,
        days=days,
        limit=limit,
        dry_run=args.dry_run,
    )

    print(
        f"扫描 {report.scanned} 条，建议 {report.suggested} 条，"
        f"保留 {report.kept} 条，跳过（已有待处理建议）{report.skipped_pending} 条，"
        f"失败 {report.failed} 条，耗时 {report.elapsed_ms}ms。"
    )

    for item in report.items:
        print(
            f"  - [{item['action']}] {item['title']} "
            f"（置信度 {item['confidence']:.2f}，证据 {item['evidence_count']} 条）"
            f"：{item['reason']}"
        )

    if args.json:
        target = Path(args.json)
        target.write_text(
            json.dumps(
                {
                    "tenant": tenant,
                    "user": args.user,
                    "days": days,
                    "dry_run": args.dry_run,
                    "report": report.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"详细结果：{target}")

    return 0 if report.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="离线跑一次梦境：核对自动抽取的记忆")
    parser.add_argument("--tenant", default=None, help="租户 ID，默认取 DEFAULT_TENANT_ID")
    parser.add_argument("--user", required=True, help="要给谁整理梦境（user_id）")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="证据窗口天数，默认取租户配置（未配置时为平台默认值）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="单次梦境最多整理条数，默认取租户配置，且不会超过该配置的上限",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印建议，不写库")
    parser.add_argument("--json", default="", help="把完整报告写成 JSON")
    args = parser.parse_args()

    async def _entry() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    return asyncio.run(_entry())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
