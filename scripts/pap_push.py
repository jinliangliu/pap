# scripts/pap_push.py
"""把本地文件交给 PAP：一条命令完成「上传 → 入库 → 验证能检索到」。

## 为什么是脚本而不是 MCP 工具

MCP 工具的参数是 JSON，传文件只能 base64 内联。人写没问题，**模型写不了**：
调用方必须逐 token 生成那串 base64，169KB 的文档就是二十多万字符。
所以「一句话把文件交给 PAP」在真实链路里一定是「模型执行一条命令、
由进程搬字节」——这个脚本就是那个进程。

服务端的对应端点是 `POST /mcp/upload`（Bearer 令牌，multipart），
和 `pap_delegate` 走同一套鉴权与同一份入库实现。

## 凭据从哪来

优先级：命令行 → 环境变量 → `~/.workbuddy/mcp.json` 里 `mcpServers.pap`。
最后一条是刻意的：令牌已经配在连接器里了，不该再抄一份到别处。

用法::

    python -m scripts.pap_push docs/xxx.md
    python -m scripts.pap_push docs/xxx.md --title "T/CGAS 007-2019" --scope internal
    python -m scripts.pap_push docs/xxx.md --verify "非民用智能燃气表 时钟电池要求"
    python -m scripts.pap_push a.md b.md --json
    python -m scripts.pap_push xxx.pdf_by_PaddleOCR-VL-1.6.json   # 自动转 Markdown 再传

`.json` 走 `scripts/ocr_json_to_md.py` 转换：服务端只收 `.md/.txt/.pdf/.docx/.html`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# 直接 `python scripts/pap_push.py …` 时 sys.path[0] 是 scripts/，项目根不在里面。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.knowledge.titles import clean_title  # noqa: E402
from scripts.ocr_json_to_md import convert

MCP_CONFIG = Path.home() / ".workbuddy" / "mcp.json"
ENDPOINT_HINT = "http://127.0.0.1:8080/mcp"

class PushError(Exception):
    """可预期的失败：凭据、网络、服务端拒绝。"""


def _from_mcp_json() -> tuple[str | None, str | None]:
    """从 WorkBuddy 的连接器配置里取端点与令牌。"""
    try:
        raw = MCP_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return None, None

    try:
        servers = json.loads(raw).get("mcpServers") or {}
    except json.JSONDecodeError:
        return None, None

    pap = servers.get("pap") or {}
    url = pap.get("url")
    auth = (pap.get("headers") or {}).get("Authorization") or ""
    token = auth.split(" ", 1)[1].strip() if " " in auth else auth.strip()

    return url or None, token or None


def resolve(args: argparse.Namespace) -> tuple[str, str]:
    file_url, file_token = _from_mcp_json()

    url = args.url or os.environ.get("PAP_MCP_URL") or file_url or ENDPOINT_HINT
    token = args.token or os.environ.get("PAP_TOKEN") or file_token

    if not token:
        raise PushError(
            "没有可用的令牌。用 --token 指定，或设 PAP_TOKEN，"
            f"或在 {MCP_CONFIG} 里配好 mcpServers.pap。"
        )

    return url, token


def upload_url(mcp_url: str) -> str:
    """`.../mcp` → `.../mcp/upload`。已经是上传地址就原样返回。"""
    base = mcp_url.rstrip("/")

    if base.endswith("/upload"):
        return base

    return f"{base}/upload"


def _payload_for(path: Path) -> tuple[str, bytes]:
    """读要上传的字节，返回 (文件名, 正文)。

    `.json` 先转 Markdown：服务端只收 `.md/.txt/.pdf/.docx/.html`，而现场拿到的
    扫描件产物往往是 PaddleOCR-VL 的 JSON。这一步放在 CLI 里而不是服务端，
    是因为转换规则属于「交给 PAP 之前的整理」，不该让入库端认识 OCR 格式。
    """
    if path.suffix.lower() != ".json":
        return path.name, path.read_bytes()

    try:
        markdown, _ = convert(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise PushError(f"{path.name} 不是可用的 PaddleOCR-VL 结果：{exc}") from exc

    return path.with_suffix(".md").name, markdown.encode("utf-8")


def _default_title(path: Path) -> str:
    """去掉 OCR 工具留下的长尾巴。

    `GB 27791-2011 城镇燃气调压箱.pdf_by_PaddleOCR-VL-1.6.json`
    → `GB 27791-2011 城镇燃气调压箱`。标题会进检索的标题加权，长尾是纯噪声。

    规则本身在 `app/knowledge/titles.py`：服务端入库时也会清一遍，
    这里只是让上传时就带上干净名字（省得列表里先脏一下再变）。
    """
    return clean_title(path.stem)


def push(
    client: httpx.Client,
    *,
    url: str,
    token: str,
    path: Path,
    title: str | None,
    scope: str | None,
) -> dict[str, Any]:
    if not path.exists():
        raise PushError(f"文件不存在：{path}")
    if path.is_dir():
        raise PushError(f"这是一个目录，请指定文件：{path}")

    data: dict[str, str] = {}
    if title:
        data["title"] = title
    if scope:
        data["access_scope"] = scope

    filename, body = _payload_for(path)

    if "title" not in data:
        # 服务端的兜底标题是 `Path(filename).stem`，用在 OCR 产物上会变成
        # 「GB 27791-2011 城镇燃气调压箱.pdf_by_PaddleOCR-VL-1.6」。转换时顺手给它一个干净的名字。
        data["title"] = _default_title(path)

    files = {"file": (filename, body, "application/octet-stream")}
    # 令牌显式带上，不依赖调用方在 client 上设过默认头。
    # 这里漏过一次：client 建好之后 `push` 忘了加头，上传变成匿名请求，
    # 服务端回 401，而 CLI 的报错写成「令牌无效或已撤销」——把人引到重签令牌上，
    # 真正的原因（这个请求根本没带令牌）被那句文案盖住了。
    response = client.post(
        url,
        files=files,
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )

    if response.status_code == 401:
        raise PushError(
            "401：服务端没认这枚令牌。两种可能——令牌已撤销或过期，"
            "或者这个请求根本没带上令牌（检查 --token / PAP_TOKEN / mcp.json）。"
        )
    if response.status_code == 403:
        raise PushError(
            f"403：{_detail(response)}——上传需要 knowledge:write 权限或平台管理员身份。"
        )
    if response.status_code == 404:
        raise PushError(
            f"404：{url} 不存在。多半是入向通道没开（MCP_INBOUND_ENABLED）"
            "或反代没把 /mcp/upload 转过去。"
        )
    if response.status_code >= 400:
        raise PushError(f"{response.status_code}：{_detail(response)}")

    try:
        return response.json()
    except ValueError as exc:
        raise PushError(f"返回体不是 JSON：{response.text[:200]}") from exc


def delegate(
    client: httpx.Client,
    *,
    mcp_url: str,
    token: str,
    task: str,
    wait_seconds: float,
) -> dict[str, Any]:
    """调一次 `pap_delegate`，用来确认 PAP 真的能检索到刚入库的内容。"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "pap_delegate",
            "arguments": {"task": task, "wait_seconds": wait_seconds, "allow_write": False},
        },
    }

    response = client.post(
        mcp_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        timeout=max(30.0, wait_seconds + 20),
    )

    if response.status_code == 401:
        raise PushError("验证环节 401：令牌失效。")

    body = response.json()

    if "error" in body:
        raise PushError(f"验证环节调用失败：{body['error']}")

    for item in (body.get("result") or {}).get("content") or []:
        if item.get("type") == "text":
            return json.loads(item["text"])

    raise PushError("验证环节没有拿到可解析的返回体。")


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] or "无返回内容"

    return str(payload.get("detail") or payload.get("message") or payload)[:300]


def _print_result(result: dict[str, Any], *, path: Path) -> None:
    doc = result.get("document") or {}
    size_kb = result.get("size_bytes", 0) / 1024
    # 上传名可能与本地名不同（OCR JSON 会转成 .md），以服务端收到的为准。
    shown = doc.get("filename") or path.name

    print(f"已交给 PAP：{shown}")
    print(f"  标题    {doc.get('title')}")
    print(f"  切片    {doc.get('chunk_count')} 片 · 分级 {doc.get('access_scope')} · {size_kb:.1f} KB")
    print(f"  动作    {result.get('action')}")
    print(f"  文档 id {doc.get('id')}")
    print(f"  {result.get('message')}")


def build_parser() -> argparse.ArgumentParser:
    """参数解析单独一个函数，测试要复用它构造同样的 Namespace。"""
    parser = argparse.ArgumentParser(
        prog="pap_push",
        description="把本地文件交给 PAP 入库（上传 + 入库 + 可选检索验证）",
    )
    parser.add_argument("files", nargs="+", help="要交给 PAP 的文件")
    parser.add_argument("--title", help="文档标题（单文件时生效，默认取文件名）")
    parser.add_argument(
        "--scope",
        default=None,
        help="分级：internal / ... 默认沿用服务端的 internal",
    )
    parser.add_argument("--url", help=f"MCP 端点，默认取 mcp.json 或 {ENDPOINT_HINT}")
    parser.add_argument("--token", help="个人访问令牌，默认取 mcp.json 里的那份")
    parser.add_argument(
        "--verify",
        metavar="QUESTION",
        help="入库后用这个问题问一次 PAP，确认它真的检索得到",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（给程序用）")
    parser.add_argument("--timeout", type=float, default=180.0, help="上传超时秒数")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        url, token = resolve(args)
    except PushError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    target = upload_url(url)
    results: list[dict[str, Any]] = []

    try:
        with httpx.Client(
            timeout=args.timeout,
            # 默认头也设一份：`push` 与 `delegate` 各自显式带了令牌，
            # 但将来加第三个请求时不该再靠记性。
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            for raw in args.files:
                path = Path(raw)
                title = args.title if len(args.files) == 1 else None
                result = push(
                    client,
                    url=target,
                    token=token,
                    path=path,
                    title=title,
                    scope=args.scope,
                )
                results.append({"file": str(path), **result})

                if not args.json:
                    _print_result(result, path=path)

            if args.verify:
                answer = delegate(
                    client,
                    mcp_url=url,
                    token=token,
                    task=f"用知识库回答：{args.verify}。只读查询，不要建单，不要修改任何数据。",
                    wait_seconds=60.0,
                )
                results.append({"verify": answer})

                if not args.json:
                    print("\n检索验证：")
                    print(f"  status  {answer.get('status')}")
                    for cite in (answer.get("citations") or [])[:3]:
                        print(f"  引用    {cite.get('title')} · {cite.get('section')}")
                    text = (answer.get("answer") or "").strip().replace("\n", " ")
                    print(f"  回答    {text[:220]}")

                    if answer.get("status") != "completed":
                        print(f"  注意    {answer.get('hint')}", file=sys.stderr)
    except PushError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"网络错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("检查端点是否可达（隧道 / 域名），以及 /mcp 是否已开放。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
