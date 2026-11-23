# scripts/backfill_mcp_tool_descriptions.py
"""把远端工具的说明补进已接入的接入项。

## 为什么需要它

批量接入时前端只提交三个字段（远端名、本地名、风险级），后端照抄
`item["description"]`——空。于是 `mcp_registered_tools.description` 全是空串，
连带两处一起坏：

- 工具页每个 MCP 工具的详情都显示「（无描述）」；
- **工具定义里的 `description` 也是空**（`_build_definition` 直接取
  `spec.description`），模型看到的只有 `fc_querypressure` 这样的名字，
  只能靠词形猜用途。

`bind_mcp_tools` 已改成「调用方没给就取远端 schema 里那句」，但只对新接入的
生效。存量行得跑一次这个脚本。

## 判据与边界

- 只补 `description` 为空的行，**不覆盖**人工写过的说明；
- 远端已经没有这个工具（改名/下线）就跳过并如实报出来，不猜；
- 凭据按租户取：连接器是平台级、凭据是租户级，任取一个本租户凭据只为
  拿到远端 schema；拿不到就跳过这个连接器，不报错退出。
- `--dry-run` 只打印将要写什么，不落库、不动注册表。
- **补完要重启服务**：注册表是进程内的，启动时才按库重新注册。脚本自己进程里
  注册一遍没有任何用（那是另一个进程的网关），所以这里不碰注册表。

用法::

    uv run python -m scripts.backfill_mcp_tool_descriptions --dry-run
    uv run python -m scripts.backfill_mcp_tool_descriptions
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.connector import Connector, ConnectorKind
from app.models.mcp_registered import McpRegisteredTool
from app.models.tenant import Tenant
from app.services import connector_service
from app.tenancy.context import tenant_scope


async def _secrets_by_connector(session) -> dict[int, str]:
    """每个连接器任取一份可用凭据（不区分租户，只为拉 schema）。"""
    secrets: dict[int, str] = {}
    tenants = (await session.scalars(select(Tenant).order_by(Tenant.id))).all()

    for tenant in tenants:
        # 逐个租户进出，不能嵌套：`tenant_scope` 不允许同一条执行流里换租户。
        with tenant_scope(tenant.id):
            credentials = await connector_service.credential_map(
                session, tenant_id=tenant.id
            )
        for connector_id, credential in credentials.items():
            if credential.secret and connector_id not in secrets:
                secrets[connector_id] = credential.secret

    return secrets


async def backfill(*, dry_run: bool) -> int:
    updated = 0

    async with async_session_factory() as session:
        connectors = (
            await session.scalars(
                select(Connector).where(Connector.kind == ConnectorKind.MCP)
            )
        ).all()
        secrets = await _secrets_by_connector(session)

        for connector in connectors:
            rows = (
                await session.scalars(
                    select(McpRegisteredTool)
                    .where(McpRegisteredTool.connector_id == connector.id)
                    .order_by(McpRegisteredTool.id)
                )
            ).all()
            empty = [row for row in rows if not (row.description or "").strip()]
            if not empty:
                print(f"[{connector.name}] 无需补：{len(rows)} 条都有说明")
                continue

            secret = secrets.get(connector.id)
            try:
                tools = await connector_service.discover_remote(
                    connector=connector, secret=secret
                )
            except Exception as exc:  # 远端不通不该中断其它连接器
                print(f"[{connector.name}] 拉清单失败，跳过：{exc}")
                continue

            by_name = {tool.name: tool for tool in tools}
            hit = 0

            for row in empty:
                descriptor = by_name.get(row.remote_name)
                if descriptor is None:
                    print(
                        f"[{connector.name}] 跳过 {row.local_name}："
                        f"远端已没有 {row.remote_name}"
                    )
                    continue

                description = (descriptor.description or "").strip()
                if not description:
                    print(f"[{connector.name}] 跳过 {row.local_name}：远端也没说明")
                    continue

                print(f"[{connector.name}] {row.local_name} ← {description[:60]}")
                hit += 1

                if dry_run:
                    continue

                row.description = description
                updated += 1

            if dry_run or not hit:
                continue

            await session.commit()

    print(f"完成：补了 {updated} 条说明" + ("（dry-run，未写库）" if dry_run else ""))
    if updated and not dry_run:
        print("记得重启服务：注册表在进程内，启动时才按库重新注册（描述不会自己生效）。")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐 MCP 接入项的远端说明")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
