# scripts/eval_knowledge.py
"""知识检索 golden set 评测。

两种模式，用途不同，不要混：

- ``local``（默认）：直接扫 ``data/knowledge`` 构造 BM25 索引，不连数据库、
  不调模型。量的是**召回上限**——纯词面检索能不能把正确片段取进 top-k。
  可以进 CI，可以用 ``--no-boost`` / ``--expand`` 做 A/B 对照。
- ``http``：打运行中服务的 ``/knowledge/search``，量的是**端到端结果**，
  包含 ACL 过滤与语义重筛。适合上线前后在同一台机器上对照。

用法：
    uv run python -m scripts.eval_knowledge
    uv run python -m scripts.eval_knowledge --no-boost          # 关掉字段加权对照
    uv run python -m scripts.eval_knowledge --expand            # 打开查询扩展对照
    uv run python -m scripts.eval_knowledge --mode http --base-url http://127.0.0.1:8000/api/v1
    uv run python -m scripts.eval_knowledge --json report.json --fail-under 0.75

指标定义见 ``summarize``。评测集里的 ``expect_found=false`` 用例不是「失败」，
而是「库里本就没有答案」；这类用例命中才是问题（假阳性）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# 判分与汇总只有一份实现（app/knowledge/evaluation.py）：
# 脚本与治理页各写一套的话，同一批用例在命令行是 0.62、在页面是 0.55，
# 而且没人能说清哪个对。这里只保留「从哪儿取语料」的差别。
from app.knowledge.evaluation import CaseResult, score_case, summarize

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests/fixtures" / "knowledge_golden.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")

    if not isinstance(cases, list) or not cases:
        raise ValueError(f"评测集格式错误：{path} 缺少 cases 数组")

    for case in cases:
        if not case.get("id") or not case.get("query"):
            raise ValueError(f"评测集存在缺少 id/query 的用例：{case}")
        if case.get("expect_found") and not case.get("relevant"):
            raise ValueError(f"可回答用例必须标注 relevant：{case['id']}")

    return cases


def run_local(args: argparse.Namespace, cases: list[dict[str, Any]]) -> list[CaseResult]:
    """纯 BM25 召回评测：与线上 ``_retrieve`` 的召回阶段同参（关掉两层阈值）。"""
    from app.core.config import settings
    from app.knowledge.ingest import build_index
    from app.knowledge.retriever import KnowledgeRetriever

    root = Path(args.knowledge_dir) if args.knowledge_dir else REPO_ROOT / "data/knowledge"
    records, report = build_index(str(root))
    # 基线要可复现：扩展显式由 --expand 决定，不随服务器上的全局开关漂移；
    # 字段加权默认跟随当前配置，--no-boost 可固定为 0 做对照。
    title_boost = 0 if args.no_boost else settings.knowledge_title_boost
    section_boost = 0 if args.no_boost else settings.knowledge_section_boost
    retriever = KnowledgeRetriever(records, title_boost=title_boost, section_boost=section_boost)
    results: list[CaseResult] = []

    print(f"语料目录：{root}（{report.documents} 份文档 / {report.chunks} 个切片）")
    print(
        f"字段加权：title={title_boost} section={section_boost} | "
        f"查询扩展：{'开' if args.expand else '关'} | 语义重筛：不含"
    )

    for case in cases:
        _, citations = retriever.search(
            query=case["query"],
            allowed_scopes={"public", "internal"},
            top_k=args.k,
            min_score=0,
            min_relative_score=0,
            query_expansion=bool(args.expand),
        )
        results.append(
            score_case(
                case,
                [
                    {"document_id": item.source_path, "section": item.section}
                    for item in citations
                ],
            )
        )

    return results


async def run_http(args: argparse.Namespace, cases: list[dict[str, Any]]) -> list[CaseResult]:
    """端到端评测：包含权限过滤与语义重筛。"""
    import httpx

    headers: dict[str, str] = {}

    if args.subject:
        headers["X-Authenticated-Subject"] = args.subject
    if args.tenant:
        headers["X-Authenticated-Tenant"] = args.tenant

    base_url = args.base_url.rstrip("/") + "/"
    results: list[CaseResult] = []

    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=args.timeout
    ) as client:
        for case in cases:
            try:
                response = await client.post(
                    "knowledge/search", json={"query": case["query"], "limit": args.k}
                )
                response.raise_for_status()
                payload = response.json()
                citations = payload.get("citations") or []
                results.append(
                    score_case(
                        case,
                        [
                            {"document_id": str(item.get("source_path", "")), "section": ""}
                            for item in citations
                        ],
                        extra={"rerank": payload.get("rerank") or {}},
                    )
                )
            except Exception as exc:  # 单条失败不中断整批评测
                results.append(
                    CaseResult(
                        case_id=str(case["id"]),
                        query=str(case["query"]),
                        expect_found=bool(case.get("expect_found", True)),
                        found=False,
                        hit=False,
                        reciprocal_rank=0.0,
                        sources=[],
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    return results


def print_report(results: list[CaseResult], metrics: dict[str, Any], verbose: bool) -> None:
    if verbose:
        print(f"\n{'用例':<34} {'期望':<6} {'命中':<6} {'RR':<6} 首条来源")
        for item in results:
            expected = "有答案" if item.expect_found else "无答案"
            hit = "是" if item.hit else "否"
            first = item.sources[0] if item.sources else "-"
            suffix = f"  [{item.error}]" if item.error else ""
            print(
                f"{item.case_id:<34} {expected:<6} {hit:<6} "
                f"{item.reciprocal_rank:<6.2f} {first}{suffix}"
            )

    print("\n汇总（Hit@k 与 MRR 只对「有答案」用例计算）")
    for key, value in metrics.items():
        print(f"  {key:<28} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="知识检索 golden set 评测")
    parser.add_argument("--mode", choices=["local", "http"], default="local")
    parser.add_argument("--golden", default=str(GOLDEN_PATH), help="评测集 JSON 路径")
    parser.add_argument("--k", type=int, default=4, help="top-k，默认 4")
    parser.add_argument("--knowledge-dir", default=None, help="local 模式的语料目录")
    parser.add_argument("--no-boost", action="store_true", help="关闭标题/章节字段加权做对照")
    parser.add_argument("--expand", action="store_true", help="打开查询同义词扩展做对照")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--subject", default="", help="http 模式的认证主体")
    parser.add_argument("--tenant", default="", help="http 模式的租户")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", default="", help="把详细结果写成 JSON 文件")
    parser.add_argument("--verbose", action="store_true", help="逐条打印结果")
    parser.add_argument(
        "--fail-under", type=float, default=None, help="Hit@k 低于该值时退出码 1（CI 门禁）"
    )
    args = parser.parse_args()
    # /knowledge/search 的 limit 被接口限制在 1..10；local 模式不受此限。
    if args.mode == "http" and not 1 <= args.k <= 10:
        parser.error("http 模式的 --k 必须在 1..10 之间（接口 limit 上限）")

    try:
        cases = load_cases(Path(args.golden))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"评测集读取失败：{exc}", file=sys.stderr)
        return 2

    print(f"模式：{args.mode} | 评测集：{args.golden} | {len(cases)} 条 | top_k={args.k}")

    if args.mode == "local":
        print("说明：local 模式不含语义重筛，量的是纯 BM25 召回上限。")
        results = run_local(args, cases)
    else:
        results = asyncio.run(run_http(args, cases))

    metrics = summarize(results, k=args.k)
    print_report(results, metrics, args.verbose)

    if args.json:
        target = Path(args.json)
        target.write_text(
            json.dumps(
                {
                    "mode": args.mode,
                    "metrics": metrics,
                    "options": {
                        "no_boost": args.no_boost,
                        "expand": args.expand,
                        "knowledge_dir": args.knowledge_dir,
                    },
                    "cases": [asdict(item) for item in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"详细结果：{target}")

    if metrics["errors"]:
        return 2
    if args.fail_under is not None and metrics["hit_at_k"] < args.fail_under:
        print(f"Hit@{args.k}={metrics['hit_at_k']} 低于门禁 {args.fail_under}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
