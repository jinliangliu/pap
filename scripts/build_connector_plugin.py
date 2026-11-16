# scripts/build_connector_plugin.py
"""把 PAP 打包成一个「连接器插件」，好让它出现在 WorkBuddy 的连接器选择器里。

**为什么要打包。** `~/.workbuddy/mcp.json` 里的自定义 MCP 服务能用，但它不进
连接器目录：`list_installed_plugins` 查不到、对话里的选择器不列它，只能靠
点名调工具。连接器插件是另一条通道——宿主会把它当「连接器」登记，
出现在目录与选择器里，也可以被单独启用/停用。

**凭据怎么带。** 连接器插件的凭据注入只认 `mcp-oauth` 与 `oneid-token` 两种
（见官方包 `tencent-docs` 的 `injectionRules`），没有「静态令牌」这一档。
但插件 `mcp.json` 的 `headers` 是 schema 明确允许的，所以这里直接把 PAT 写进
`mcp.json.headers`。代价：**令牌随包走**——这个包只能自己用，
发给别人等于把你的账号发出去。

生成的包结构（与官方连接器一致）：

    <out>/
      .codebuddy-plugin/marketplace.json
      plugins/pap/
        .codebuddy-plugin/plugin.json
        mcp.json
        ai.workbuddy/assets/icon.png

用法：

    python -m scripts.build_connector_plugin --url http://127.0.0.1:8080/mcp --token pap_xxx

输出目录默认 `deploy/connector`（在 .gitignore 里，别提交）。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

PLUGIN_NAME = "pap"
MARKETPLACE_NAME = "pap-connector"
VERSION = "1.0.0"

DESCRIPTION_ZH = (
    "把一整件事交给 PAP 多智能体平台：由它自己拆解、选智能体、调数据源，"
    "拿回结论、引用与执行轨迹。"
)
DESCRIPTION_EN = (
    "Delegate a whole task to the PAP multi-agent platform; it plans, picks agents "
    "and queries data sources on its own, returning an answer with citations and trace."
)

EXAMPLES_ZH = [
    "让 PAP 查一下深圳燃气大厦现在的压力与报警状态",
    "把「沧源二村4283 调压器出口压力偏低」这件事交给 PAP 诊断并给出处置建议",
    "让 PAP 按 TCGAS 027-2023 查一下调压箱泄漏处置的规程要求",
]
EXAMPLES_EN = [
    "Ask PAP for the current pressure and alarms at Shenzhen Gas Tower",
    "Have PAP diagnose low outlet pressure at station 4283 and suggest a fix",
    "Ask PAP what the TCGAS 027-2023 procedure says about regulator-box leaks",
]


def _png_icon(size: int = 512) -> bytes:
    """画一个纯色圆角底 + 白色「P」的方图。

    不引第三方依赖：官方包都带图标，缺图标在目录里会显示成占位方块，
    看起来像装坏了。这里手写 PNG，够用就行。
    """
    bg = (31, 41, 55)      # 深灰蓝，压在浅色与深色底上都不刺眼
    fg = (255, 255, 255)

    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            # 圆角：四角半径 96 的圆外算透明
            r = 96
            corner_x = min(x, size - 1 - x)
            corner_y = min(y, size - 1 - y)
            if corner_x < r and corner_y < r:
                dx, dy = r - corner_x, r - corner_y
                if dx * dx + dy * dy > r * r:
                    row += bytes((0, 0, 0, 0))
                    continue
            # 字母 P：竖笔 + 上半环。用几个矩形近似，够辨认。
            in_stem = 168 <= x <= 236 and 128 <= y <= 384
            in_bowl = 236 <= x <= 344 and 128 <= y <= 268
            in_hole = 252 <= x <= 330 and 152 <= y <= 244
            is_fg = in_stem or (in_bowl and not in_hole)
            row += bytes((*(fg if is_fg else bg), 255))
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _plugin_json(url: str) -> dict:
    return {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": PLUGIN_NAME,
        "version": VERSION,
        "description": DESCRIPTION_EN,
        "keywords": ["mcp"],
        "extensions": {
            "ai.workbuddy": {
                "schemaVersion": "1.0.0",
                "display": {
                    "name": "PAP 智能体平台",
                    "name_en": "PAP Agent Platform",
                    "description_zh": DESCRIPTION_ZH,
                    "description_en": DESCRIPTION_EN,
                    "examples_zh": EXAMPLES_ZH,
                    "examples_en": EXAMPLES_EN,
                },
                "policy": {
                    # 30 秒是服务端的同步等待上限，客户端必须比它宽，否则会在
                    # 边界上被切断，看到「连接中断」而不是 running + task_id。
                    "mcpServers": {PLUGIN_NAME: {"timeout": 600}},
                },
            }
        },
    }


def _mcp_json(url: str, token: str) -> dict:
    return {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            PLUGIN_NAME: {
                "type": "streamable-http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        },
    }


def _marketplace_json() -> dict:
    return {
        "name": MARKETPLACE_NAME,
        "description": "PAP 智能体平台的本地连接器市场（自用）。",
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": f"./plugins/{PLUGIN_NAME}",
                "description": DESCRIPTION_ZH,
                "version": VERSION,
            }
        ],
    }


def build(*, out: Path, url: str, token: str) -> Path:
    root = out / MARKETPLACE_NAME
    plugin = root / "plugins" / PLUGIN_NAME
    (root / ".codebuddy-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".codebuddy-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / "ai.workbuddy" / "assets").mkdir(parents=True, exist_ok=True)

    def write(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    write(root / ".codebuddy-plugin" / "marketplace.json", _marketplace_json())
    write(plugin / ".codebuddy-plugin" / "plugin.json", _plugin_json(url))
    write(plugin / "mcp.json", _mcp_json(url, token))
    (plugin / "ai.workbuddy" / "assets" / "icon.png").write_bytes(_png_icon())
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 PAP 连接器插件")
    parser.add_argument("--url", required=True, help="MCP 端点，如 http://127.0.0.1:8080/mcp")
    parser.add_argument("--token", required=True, help="个人访问令牌（pap_ 开头）")
    parser.add_argument(
        "--out",
        default="deploy/connector",
        help="输出目录，默认 deploy/connector（已 gitignore）",
    )
    args = parser.parse_args(argv)

    token = args.token.strip()
    if not token.startswith("pap_"):
        print("令牌看起来不对：应当以 pap_ 开头。", file=sys.stderr)
        return 2
    if not args.url.startswith(("http://", "https://")):
        print("url 必须以 http:// 或 https:// 开头。", file=sys.stderr)
        return 2
    if args.url.startswith("http://") and "127.0.0.1" not in args.url and "localhost" not in args.url:
        print(
            "警告：明文 http 指向公网地址，令牌会以明文穿过网络。"
            "建议走 SSH 隧道（127.0.0.1）或给 nginx 上 TLS。",
            file=sys.stderr,
        )

    root = build(out=Path(args.out), url=args.url.strip(), token=token)
    print(f"已生成：{root}")
    for item in sorted(root.rglob("*")):
        if item.is_file():
            print(f"  {item.relative_to(root)}")
    print()
    print("下一步：")
    print(f"  1. 复制整个目录到 ~/.workbuddy/plugins/marketplaces/{MARKETPLACE_NAME}/")
    print("  2. 关掉旧的 mcp.json 里同名条目（否则两个 pap 会撞工具名）")
    print("  3. 重启 WorkBuddy，在「目录市场」里安装并启用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
