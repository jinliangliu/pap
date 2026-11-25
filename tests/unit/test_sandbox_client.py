# tests/unit/test_sandbox_client.py
"""沙箱客户端的协议适配：同步、异步轮询、拒绝、超时与截断。"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.sandbox_client import (
    SandboxClient,
    SandboxRejected,
    SandboxTimeout,
    SandboxUnavailable,
)


def _build(handler, **kwargs) -> SandboxClient:  # noqa: ANN001
    client = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        timeout_seconds=kwargs.pop("timeout_seconds", 5),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        max_output_chars=kwargs.pop("max_output_chars", 1000),
    )
    # 直接替换底层 transport，避免真实网络。
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://sandbox.test",
    )
    return client


async def test_execute_synchronous_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/execute"
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["idempotency-key"] == "approval:t1:r1"
        # 租户必须随请求下发，否则沙箱无法隔离执行记录。
        assert request.headers["x-sandbox-tenant"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "execution_id": "exec-1",
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "42\n",
                "stderr": "",
                "duration_ms": 12,
                "backend": "runsc",
            },
        )

    client = _build(handler)
    result = await client.execute(
        code="print(42)",
        language="python",
        idempotency_key="approval:t1:r1",
        tenant_id="tenant-a",
    )

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.stdout == "42\n"
    assert result.succeeded is True
    assert result.execution_id == "exec-1"
    assert result.backend == "runsc"


async def test_execute_polls_until_terminal():
    """202 表示任务已受理，必须轮询到终态才算完成。"""
    polls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"execution_id": "exec-2", "status": "queued"})

        polls.append(request.url.path)

        if len(polls) < 2:
            return httpx.Response(200, json={"status": "running"})

        return httpx.Response(
            200,
            json={"status": "succeeded", "exit_code": 0, "stdout": "done"},
        )

    client = _build(handler)
    result = await client.execute(code="print('x')", language="python")

    assert result.status == "succeeded"
    assert result.stdout == "done"
    assert polls == ["/v1/executions/exec-2", "/v1/executions/exec-2"]


async def test_execute_rejects_with_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"message": "代码超过沙箱上限"})

    client = _build(handler)

    with pytest.raises(SandboxRejected, match="代码超过沙箱上限"):
        await client.execute(code="x" * 10, language="python")


async def test_execute_server_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream busy")

    client = _build(handler)

    with pytest.raises(SandboxUnavailable):
        await client.execute(code="print(1)", language="python")


async def test_execute_connect_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _build(handler)

    with pytest.raises(SandboxUnavailable):
        await client.execute(code="print(1)", language="python")


async def test_execute_gives_up_after_budget():
    """永远 running 的任务不能无限等待。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"execution_id": "exec-3", "status": "queued"})
        return httpx.Response(200, json={"status": "running"})

    client = _build(handler, timeout_seconds=0.2, poll_interval_seconds=0.02)

    with pytest.raises(SandboxTimeout):
        await client.execute(code="print(1)", language="python")


async def test_output_is_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "a" * 500,
                "stderr": "b" * 500,
            },
        )

    client = _build(handler, max_output_chars=10)
    result = await client.execute(code="print('a')", language="python")

    assert result.truncated is True
    assert result.stdout.startswith("a" * 10)
    assert "已截断" in result.stdout
    assert len(result.stderr) < 60


async def test_pending_without_execution_id_fails_fast():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "queued"})

    client = _build(handler)

    with pytest.raises(Exception, match="execution_id"):
        await client.execute(code="print(1)", language="python")


async def test_health_reflects_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health"
        return httpx.Response(200, json={"status": "ok"})

    assert await _build(handler).health() is True


async def test_health_false_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert await _build(handler).health() is False


async def test_get_execution_reads_record():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/executions/exec-7"
        assert request.headers["x-sandbox-tenant"] == "tenant-a"
        return httpx.Response(
            200,
            json={"status": "succeeded", "exit_code": 0, "stdout": "ok"},
        )

    result = await _build(handler).get_execution("exec-7", tenant_id="tenant-a")

    assert result.stdout == "ok"
    assert result.succeeded is True


async def test_get_execution_missing_record_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "记录已过期"})

    with pytest.raises(SandboxRejected, match="记录已过期"):
        await _build(handler).get_execution("exec-gone")


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (38, 38),   # 测到了，消耗 38ms
        (0, 0),     # 测到了，确实没消耗 CPU——与 None 不能混
        (None, None),
        ("not-a-number", None),  # 字段类型不对时按「没测到」处理
    ],
)
async def test_cpu_ms_preserves_missing_vs_zero(reported, expected):
    """cpu_ms 的 null 与 0 是两种计量事实，解析时不能合并。"""
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "execution_id": "exec-cpu",
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 5,
        }
        if reported is not None or request.method == "POST":
            payload["cpu_ms"] = reported
        return httpx.Response(200, json=payload)

    result = await _build(handler).execute(code="pass", language="python")

    assert result.cpu_ms == expected
