# tests/unit/test_custom_http_tools.py
"""自定义 HTTP Tool 的配置校验与执行器行为。

重点不在「能不能发请求」，而在「配置错误会不会被提前拦住」：
URL 带着凭据、参数位置写错、声明了 path 却没占位符，这些都必须在
保存时变成 400，而不是等模型调用时收到一个看不懂的失败。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models.approval import RiskLevel
from app.tools import custom_http
from app.tools.custom_http import (
    CustomToolConfigError,
    HttpParamSpec,
    HttpToolConfig,
    build_http_definition,
    build_input_schema,
    parse_params,
    validate_url,
)
from app.tools.types import ToolExecutionContext

pytestmark = pytest.mark.anyio

CONTEXT = ToolExecutionContext(
    tenant_id="default",
    user_id="u-1",
    thread_id="t-1",
    run_id="r-1",
    request_id="req-1",
)


# ---------------- URL 校验 ----------------


def test_validate_url_accepts_http_and_https():
    assert validate_url(" https://api.example.com/items ") == "https://api.example.com/items"
    assert validate_url("http://10.0.0.8:8080/health") == "http://10.0.0.8:8080/health"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "",
        "not-a-url",
    ],
)
def test_validate_url_rejects_bad_scheme(url):
    with pytest.raises(CustomToolConfigError):
        validate_url(url)


def test_validate_url_rejects_credentials_in_url():
    """凭据写进 URL 会被日志和错误信息带出去，必须拒绝。"""
    with pytest.raises(CustomToolConfigError):
        validate_url("https://user:pass@api.example.com/items")


def test_validate_url_rejects_metadata_and_link_local():
    with pytest.raises(CustomToolConfigError):
        validate_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(CustomToolConfigError):
        validate_url("http://169.254.1.5/internal")


# ---------------- 参数定义 ----------------


def test_parse_params_accepts_full_shape():
    specs = parse_params(
        [
            {
                "name": "station_id",
                "location": "path",
                "type": "string",
                "required": True,
                "description": "站点",
            },
            {"name": "limit", "location": "query", "type": "integer", "required": False},
        ]
    )

    assert [s.name for s in specs] == ["station_id", "limit"]
    assert specs[0].location == "path"
    assert specs[1].type == "integer"
    assert specs[1].required is False


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-list",
        [{"name": "x", "location": "cookie"}],
        [{"name": "x", "type": "object"}],
        [{"name": "x"}, {"name": "x"}],
        [{"location": "query"}],
    ],
)
def test_parse_params_rejects_invalid(raw):
    with pytest.raises(CustomToolConfigError):
        parse_params(raw)


def test_build_input_schema_marks_required_and_closes_object():
    schema = build_input_schema(
        (
            HttpParamSpec(name="id", location="path", type="string", required=True),
            HttpParamSpec(name="q", location="query", type="string", required=False),
        )
    )

    assert schema["required"] == ["id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["q"]["type"] == "string"


def test_schema_allows_empty_params():
    schema = build_input_schema(())
    assert schema["properties"] == {}
    assert "required" not in schema


# ---------------- 执行器 ----------------


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        self.requests.append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        )
        if self.error is not None:
            raise self.error
        return self.response


def _config(**overrides) -> HttpToolConfig:
    base = dict(
        name="work_order_query",
        display_name="工单查询",
        description="查询工单",
        method="GET",
        url="https://api.example.com/orders",
        risk_level=RiskLevel.READ,
        timeout_seconds=10,
        params=(),
        headers={},
    )
    base.update(overrides)
    return HttpToolConfig(**base)


def _install_client(monkeypatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(custom_http.httpx, "AsyncClient", lambda **kwargs: fake)


async def test_handler_returns_success_for_2xx(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(200, {"items": [1, 2]}))
    _install_client(monkeypatch, fake)

    handler = build_http_definition(_config()).handler
    result = await handler(CONTEXT, {})

    assert result.success is True
    assert result.result["status_code"] == 200
    assert result.result["data"] == {"items": [1, 2]}
    assert fake.requests[0]["method"] == "GET"


async def test_handler_maps_path_query_and_body(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(201, {"ok": True}))
    _install_client(monkeypatch, fake)

    config = _config(
        method="POST",
        url="https://api.example.com/stations/{station_id}/orders",
        params=(
            HttpParamSpec(name="station_id", location="path", required=True),
            HttpParamSpec(name="limit", location="query", type="integer"),
            HttpParamSpec(name="note", location="body"),
        ),
    )

    result = await build_http_definition(config).handler(
        CONTEXT, {"station_id": "s-1", "limit": 5, "note": "现场"}
    )

    request = fake.requests[0]
    assert request["url"] == "https://api.example.com/stations/s-1/orders"
    assert request["params"] == {"limit": 5}
    assert request["json"] == {"note": "现场"}
    assert result.success is True


async def test_handler_fails_when_path_param_missing(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(200, {}))
    _install_client(monkeypatch, fake)

    config = _config(
        url="https://api.example.com/orders/{id}",
        params=(HttpParamSpec(name="id", location="path"),),
    )

    result = await build_http_definition(config).handler(CONTEXT, {})

    assert result.success is False
    assert result.error_code == "CUSTOM_TOOL_BAD_PATH_PARAM"
    # 参数都没凑齐就不该发出请求。
    assert fake.requests == []


async def test_handler_reports_http_error_and_retryability(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(503, {"error": "busy"}))
    _install_client(monkeypatch, fake)

    result = await build_http_definition(_config()).handler(CONTEXT, {})

    assert result.success is False
    assert result.error_code == "CUSTOM_TOOL_HTTP_503"
    assert result.retryable is True


async def test_handler_does_not_retry_writes(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(502, {}))
    _install_client(monkeypatch, fake)

    result = await build_http_definition(_config(method="POST")).handler(CONTEXT, {})

    assert result.success is False
    # 写操作即使 502 也不标可重试：网络与网关错误都不代表没写进去。
    assert result.retryable is False


async def test_handler_truncates_long_body(monkeypatch):
    long_text = "x" * (custom_http.MAX_RESULT_CHARS + 100)
    fake = _FakeClient(response=_FakeResponse(200, {"big": long_text}))
    _install_client(monkeypatch, fake)

    result = await build_http_definition(_config()).handler(CONTEXT, {})

    assert result.result["truncated"] is True
    assert len(json.dumps(result.result["data"], ensure_ascii=False)) <= (
        custom_http.MAX_RESULT_CHARS + 10
    )


async def test_handler_maps_timeout(monkeypatch):
    fake = _FakeClient(error=httpx.ReadTimeout("slow"))
    _install_client(monkeypatch, fake)

    result = await build_http_definition(_config()).handler(CONTEXT, {})

    assert result.success is False
    assert result.error_code == "CUSTOM_TOOL_TIMEOUT"
    assert result.retryable is True


async def test_handler_uses_context_credential_as_bearer(monkeypatch):
    fake = _FakeClient(response=_FakeResponse(200, {}))
    _install_client(monkeypatch, fake)

    from dataclasses import replace

    await build_http_definition(_config()).handler(
        replace(CONTEXT, credential="tok-123"), {}
    )

    assert fake.requests[0]["headers"] == {"Authorization": "Bearer tok-123"}
