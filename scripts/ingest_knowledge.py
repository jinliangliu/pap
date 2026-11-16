# scripts/ingest_knowledge.py
"""重建知识库索引（写入 PostgreSQL）。

切片落在 documents / document_chunks，运行时检索不再依赖本地 JSON；
各实例按索引指纹热加载，入库后无需重启服务。

用法：
    uv run python -m scripts.ingest_knowledge
    uv run python -m scripts.ingest_knowledge --tenant default
    uv run python -m scripts.ingest_knowledge --export-json   # 额外留一份快照
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.db.session import dispose_engine
from app.knowledge.ingest import KnowledgeIngestError, rebuild


async def _run(args: argparse.Namespace) -> int:
    print(f"知识目录：{settings.knowledge_dir}")
    print(f"目标租户：{args.tenant or settings.default_tenant_id}")

    try:
        report = await rebuild(
            tenant_id=args.tenant,
            export_json=args.export_json,
        )
    except KnowledgeIngestError as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return 1

    print(f"已导入 {report.documents} 份文档，{report.chunks} 个片段。")
    print(f"写入位置：{report.target}")

    if report.export_path:
        print(f"JSON 快照：{report.export_path}")

    if report.skipped:
        print("跳过：")
        for item in report.skipped:
            print(f"  - {item}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重建知识库索引")
    parser.add_argument("--tenant", default=None, help="租户 ID，默认取 DEFAULT_TENANT_ID")
    parser.add_argument(
        "--export-json",
        action="store_true",
        help="额外导出一份 JSON 快照（不参与运行时检索）",
    )
    args = parser.parse_args()

    async def _entry() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    return asyncio.run(_entry())


if __name__ == "__main__":
    raise SystemExit(main())
