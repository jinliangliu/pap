"""`pap_push` 的请求形态：**必须带令牌**，否则服务端只会回一个 401。

这组测试的由来：CLI 第一次真跑就 401，而代码看起来没问题——`push()` 里
`client.post` 忘了 `headers`，令牌只存在于参数里。自检脚本验的是服务端，
单测验的是工具层，**没有人验 CLI 自己发出的请求长什么样**。
所以这里断言的是「发出去的字节」，不是返回值。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts import pap_push

TOKEN = "pap_unit_test_token_value"


def _client(captured: list[httpx.Request], *, status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            status_code,
            json={
                "document": {
                    "id": "doc-1",
                    "title": "样例",
                    "filename": "sample.md",
                    "chunk_count": 3,
                    "access_scope": "internal",
                    "status": "ready",
                    "source_type": "upload",
                },
                "action": "created",
                "size_bytes": 10,
                "message": "已入库",
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_push_sends_authorization_header(tmp_path: Path):
    target = tmp_path / "sample.md"
    target.write_text("# 样例\n", encoding="utf-8")
    captured: list[httpx.Request] = []

    with _client(captured) as client:
        result = pap_push.push(
            client,
            url="http://127.0.0.1:8080/mcp/upload",
            token=TOKEN,
            path=target,
            title="样例",
            scope=None,
        )

    assert result["action"] == "created"
    assert len(captured) == 1

    request = captured[0]
    # 这两条就是当初漏掉的那件事。
    assert request.headers.get("authorization") == f"Bearer {TOKEN}"
    assert request.headers.get("content-type", "").startswith("multipart/form-data")

    body = request.read().decode("utf-8", errors="replace")
    assert 'name="file"' in body
    assert "sample.md" in body
    assert 'name="title"' in body


def test_push_401_mentions_missing_header(tmp_path: Path):
    """401 不能只让人去重签令牌——也可能是压根没带。

    最初那句「令牌无效或已撤销」把真正的原因盖住了，实测时多绕了一圈。
    """
    target = tmp_path / "sample.md"
    target.write_text("# 样例\n", encoding="utf-8")
    captured: list[httpx.Request] = []

    with _client(captured, status_code=401) as client:
        with pytest.raises(pap_push.PushError) as excinfo:
            pap_push.push(
                client,
                url="http://127.0.0.1:8080/mcp/upload",
                token=TOKEN,
                path=target,
                title=None,
                scope=None,
            )

    assert "没带上" in str(excinfo.value)


def test_resolve_prefers_explicit_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pap": {
                        "url": "http://from-config/mcp",
                        "headers": {"Authorization": "Bearer pap_from_config"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pap_push, "MCP_CONFIG", config)
    monkeypatch.delenv("PAP_TOKEN", raising=False)

    args = pap_push.build_parser().parse_args(["x.md"])
    url, token = pap_push.resolve(args)
    assert token == "pap_from_config"
    assert url == "http://from-config/mcp"

    # 环境变量压过配置文件，--token 压过两者。
    monkeypatch.setenv("PAP_TOKEN", "pap_from_env")
    _, token = pap_push.resolve(pap_push.build_parser().parse_args(["x.md"]))
    assert token == "pap_from_env"

    _, token = pap_push.resolve(
        pap_push.build_parser().parse_args(["x.md", "--token", "pap_from_flag"])
    )
    assert token == "pap_from_flag"


def test_upload_url_appends_one_segment():
    """端点带不带尾斜杠都要落到 `/upload`，别拼出 `//upload`。"""
    assert pap_push.upload_url("http://h/mcp") == "http://h/mcp/upload"
    assert pap_push.upload_url("http://h/mcp/") == "http://h/mcp/upload"


def _ocr_json(tmp_path: Path, name: str = "GB 1-2020 样例.pdf_by_PaddleOCR-VL-1.6.json") -> Path:
    target = tmp_path / name
    target.write_text(
        json.dumps(
            [
                {
                    "markdown": {"text": "# 标准\n\n## 1 范围\n\n正文", "images": {}},
                    "prunedResult": {
                        "parsing_res_list": [
                            {"block_label": "header", "block_content": "标准资料网"},
                            {"block_label": "header", "block_content": "标准资料网"},
                        ]
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target


def test_ocr_json_is_converted_before_upload(tmp_path: Path):
    """服务端不收 .json，CLI 得先转成 Markdown。

    「一句话把文件交给 PAP」的真实形态是「模型执行一条命令」，
    而手上拿到的往往是 PaddleOCR-VL 的 JSON——不能要求人先手动转一遍。
    """
    target = _ocr_json(tmp_path)
    captured: list[httpx.Request] = []

    with _client(captured) as client:
        pap_push.push(
            client,
            url="http://127.0.0.1:8080/mcp/upload",
            token=TOKEN,
            path=target,
            title=None,
            scope=None,
        )

    body = captured[0].read().decode("utf-8", errors="replace")

    # 发出去的文件名是 .md，正文里是转好的 Markdown 而不是 JSON。
    assert "GB 1-2020 样例.pdf_by_PaddleOCR-VL-1.6.md" in body
    assert "## 1 范围" in body
    assert "prunedResult" not in body
    # 标题去掉 OCR 尾巴，别让长尾进标题加权。
    assert 'name="title"' in body
    assert "GB 1-2020 样例" in body


def test_plain_json_is_rejected_before_the_request(tmp_path: Path):
    """不是 OCR 结果的 JSON 要当场报错，不要把空文档传上去。"""
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"debug": True}), encoding="utf-8")
    captured: list[httpx.Request] = []

    with _client(captured) as client:
        with pytest.raises(pap_push.PushError, match="PaddleOCR"):
            pap_push.push(
                client,
                url="http://127.0.0.1:8080/mcp/upload",
                token=TOKEN,
                path=target,
                title=None,
                scope=None,
            )

    assert captured == []
