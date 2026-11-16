# scripts/probe_knowledge_gaps.py
"""现场作业类问题的语料覆盖探测。

与 ``eval_knowledge.py`` 的分工，不要混：

- ``eval_knowledge`` 量的是**排序质量**——标注好的 relevant 片段能不能排进 top-k。
  它回答「检索准不准」，前提是库里已经有答案。
- 本脚本量的是**覆盖有无**——库里到底有没有能回答这类问题的内容。
  它回答「该补什么资料」，用例**没有** relevant 标注，因为补之前本就无解。

因此判据只有两条：``found`` 是否为真、``citations`` 是否非空。这**不等于答对**：
零覆盖之外还有一种坏情况是「召回到了同主题的标准条文」。所以每条都打印 top1 的
section，补完语料要人眼看一遍是不是误命中——「报警 等级 分级」曾经命中的是
《型号表示方法》，就是这种。

用法::

    uv run python -m scripts.probe_knowledge_gaps
    uv run python -m scripts.probe_knowledge_gaps --json gap.json
    uv run python -m scripts.probe_knowledge_gaps --category 泄漏与隐患处置
    uv run python -m scripts.probe_knowledge_gaps --min-covered 0.8   # 补语料验收用

补语料的正确流程：补一类 → 只跑这一类 → 看 covered 是否从 0 变成预期值，
且 top1 落在新补的那份文档上 → 再补下一类。一次补完六类会分不清是哪份资料起了作用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = REPO_ROOT / "tests/fixtures" / "knowledge_gap_probe.json"


@dataclass
class ProbeResult:
    case_id: str
    category: str
    query: str
    found: bool
    count: int
    recall: int = 0
    top_title: str = ""
    top_section: str = ""
    error: str = ""

    @property
    def covered(self) -> bool:
        return self.found and self.count > 0 and not self.error

    @property
    def kind(self) -> str:
        """三态。这个区分是本脚本的主要产出：

        - ``empty``：recall=0，查询词与语料没有交集。补语料的直接目标。
        - ``dropped``：有召回但全被重筛剔除——库里有同主题内容、只是不回答这个问题。
          「调压箱 泄漏 通风」就是这一类：召回 12 条、留下 0 条，而这是正确答案。
        - ``hit``：拿到了内容。**仍可能是误命中**，要人眼看 top1。
        """
        if self.error:
            return "error"
        if self.count > 0:
            return "hit"
        if self.recall > 0:
            return "dropped"
        return "empty"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "query": self.query,
            "found": self.found,
            "count": self.count,
            "recall": self.recall,
            "kind": self.kind,
            "top_title": self.top_title,
            "top_section": self.top_section,
            "error": self.error,
        }


def load_cases(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")

    if not isinstance(cases, list) or not cases:
        raise ValueError(f"探测集格式错误：{path} 缺少 cases 数组")

    for case in cases:
        for key in ("id", "category", "query"):
            if not case.get(key):
                raise ValueError(f"探测集存在缺少 {key} 的用例：{case}")

    return cases


async def run(args: argparse.Namespace, cases: list[dict[str, str]]) -> list[ProbeResult]:
    import httpx

    headers: dict[str, str] = {}

    if args.subject:
        headers["X-Authenticated-Subject"] = args.subject
    if args.tenant:
        headers["X-Authenticated-Tenant"] = args.tenant

    base_url = args.base_url.rstrip("/") + "/"
    results: list[ProbeResult] = []

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
            except Exception as exc:  # noqa: BLE001 - 探测要跑完全部用例再报错
                results.append(
                    ProbeResult(
                        case_id=case["id"],
                        category=case["category"],
                        query=case["query"],
                        found=False,
                        count=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            citations = payload.get("citations") or []
            first = citations[0] if citations else {}
            # 响应里的 found 只是 citations 非空的派生值（kept=0 时恒为 false），
            # 所以「召回到了几条再被剔除」只能从 rerank.candidates 读。
            rerank = payload.get("rerank") or {}

            results.append(
                ProbeResult(
                    case_id=case["id"],
                    category=case["category"],
                    query=case["query"],
                    found=bool(payload.get("found")),
                    count=len(citations),
                    recall=int(rerank.get("candidates") or 0),
                    top_title=str(first.get("title") or ""),
                    top_section=str(first.get("section") or ""),
                )
            )

    return results


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    by_category: dict[str, list[ProbeResult]] = defaultdict(list)

    for item in results:
        by_category[item.category].append(item)

    categories = {
        name: {
            "total": len(items),
            "hit": sum(1 for i in items if i.kind == "hit"),
            "dropped": sum(1 for i in items if i.kind == "dropped"),
            "empty": sum(1 for i in items if i.kind == "empty"),
            # 未覆盖的是 empty 与 dropped 两类：都拿不到可回答的内容。
            "missed": [i.case_id for i in items if not i.covered],
        }
        for name, items in by_category.items()
    }

    return {
        "cases": len(results),
        "covered": sum(1 for i in results if i.covered),
        "covered_rate": round(
            sum(1 for i in results if i.covered) / len(results), 4
        )
        if results
        else 0.0,
        # 两者要分开看：empty 是「语料里连词面都没有」（补语料是唯一解）；
        # dropped 是「有同主题内容但不回答问题」（补了对应规程后更容易命中，
        # 但也说明召回层在按词面把标准条文拉进来）。
        "hit": sum(1 for i in results if i.kind == "hit"),
        "dropped": sum(1 for i in results if i.kind == "dropped"),
        "empty": sum(1 for i in results if i.kind == "empty"),
        "errors": sum(1 for i in results if i.error),
        "by_category": categories,
    }


MARKS = {
    "hit": "中 ",
    "dropped": "扣 ",
    "empty": "空 ",
    "error": "!  ",
}


def print_report(results: list[ProbeResult], summary: dict[str, Any], k: int) -> None:
    print(f"\n探测集：{len(results)} 条 / top_k={k}")
    print("图例：空=召回即 0（语料无词面） 扣=有召回但被剔除 中=拿到内容（可能是误命中）\n")

    for item in results:
        top = f"{item.top_title} · {item.top_section}" if item.top_title else "—"
        note = f"  [{item.error}]" if item.error else ""
        print(
            f"{MARKS[item.kind]} {item.category:<10} | {item.query:<26} "
            f"→ 召回 {item.recall:<2} 留 {item.count:<2} | {top}{note}"
        )

    print("\n按类汇总：")
    for name, stat in summary["by_category"].items():
        missed = ", ".join(stat["missed"]) if stat["missed"] else "—"
        print(
            f"  {name:<12} 中 {stat['hit']}/{stat['total']} "
            f"（空 {stat['empty']} · 扣 {stat['dropped']}）   未覆盖：{missed}"
        )

    print(
        f"\n合计：{summary['covered']}/{summary['cases']} 拿到内容 "
        f"（{summary['covered_rate']:.0%}）"
        f" | 空 {summary['empty']} · 扣 {summary['dropped']} · 报错 {summary['errors']}"
    )

    if summary["hit"]:
        print(
            "\n注意：「中」只说明拿到了内容，不说明拿对了。"
            "逐行看 top1 是否落在能回答该问题的文档上——误命中会把它算成覆盖。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测知识库对现场作业类问题的覆盖")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--subject", default="pap-admin")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--k", type=int, default=4, help="与 KNOWLEDGE_TOP_K 一致更有意义")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--case-file", default=str(PROBE_PATH))
    parser.add_argument("--category", help="只跑某一类，补语料时用")
    parser.add_argument("--json", help="把逐条结果与汇总写到这个路径")
    parser.add_argument(
        "--min-covered",
        type=float,
        default=None,
        help="覆盖率低于此值则以 1 退出，用于补语料后的验收闸门",
    )
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.case_file))

    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
        if not cases:
            print(f"没有这一类：{args.category}", file=sys.stderr)
            return 2

    results = asyncio.run(run(args, cases))
    summary = summarize(results)
    print_report(results, summary, args.k)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "results": [r.to_dict() for r in results]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json}")

    if args.min_covered is not None and summary["covered_rate"] < args.min_covered:
        print(
            f"\n覆盖率 {summary['covered_rate']:.0%} 低于闸门 {args.min_covered:.0%}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
