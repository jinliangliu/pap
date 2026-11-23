# scripts/install_workflow.py
"""把仓库里的流程定义装进某个租户。

图是配置，配置要有源文件：`docs/examples/*.json` 是唯一来源，这个脚本只做
「读文件 → 编译 → 落库」，不在这里写任何图。日常改图在控制台改；要回到版本库，
从控制台导出再提交。

    uv run python -m scripts.install_workflow docs/examples/workflow-dispatch.json \\
        --name dispatch --display-name 隐患分级派单 \\
        --tenant default --user <user-uuid>

保存即编译：图不合法会在这里被拒，错误码与接口层一致（`WORKFLOW_*`）。
**图里用到的智能体必须已存在且处于启用状态**——这条校验在 `save_workflow` 里
（`_ensure_agents_available`），装之前先把 `docs/examples/workflow-dispatch.json`
里的 `Gas_Industry_Knowledge` / `field-ops` 建出来，否则会报 `WORKFLOW_UNKNOWN_AGENT`
并列出当前租户可用的标识。
同名重复执行：**图变了才 +1 修订号**（只改说明不计）——在飞的 run 不受影响，
它们读自己那份图副本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.db.session import dispose_engine
from app.services.workflow_service import WorkflowError, save_workflow


async def _install(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读不了定义文件：{exc}", file=sys.stderr)
        return 2

    try:
        row = await save_workflow(
            tenant_id=args.tenant,
            user_id=args.user,
            name=args.name,
            display_name=args.display_name or args.name,
            description=args.description,
            definition=definition,
            enabled=not args.disabled,
        )
    except WorkflowError as exc:
        # 编译期拒绝：把 code 和话原样打出来，别自己再拼一句「保存失败」。
        print(f"拒绝：{exc.code} {exc.message}", file=sys.stderr)
        return 1

    nodes = len((row.definition or {}).get("nodes") or [])
    print(f"已安装 {row.name}（{row.display_name}）rev={row.revision} nodes={nodes}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="把流程定义文件装进某个租户")
    parser.add_argument("file", help="定义 JSON 路径")
    parser.add_argument("--name", required=True, help="流程标识（/workflow 后面那个）")
    parser.add_argument("--display-name", default="", help="界面上显示的名字")
    parser.add_argument("--description", default=None)
    parser.add_argument("--tenant", required=True, help="租户 id")
    parser.add_argument("--user", required=True, help="created_by，填用户 uuid")
    parser.add_argument(
        "--disabled", action="store_true", help="装进去但先停用（不接 /workflow）"
    )
    args = parser.parse_args()

    async def run() -> int:
        try:
            return await _install(args)
        finally:
            await dispose_engine()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
