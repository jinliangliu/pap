# tests/integration/test_sandbox_tool.py
"""execute_code 工具的注册条件、参数校验与结果语义。"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.integrations.sandbox_client import (
    SandboxRejected,
    SandboxResult,
    SandboxTimeout,
    SandboxUnavailable,
)
from app.models.approval import RiskLevel
from app.tools import sandbox_tools
from app.tools.sandbox_tools import (
    build_sandbox_tools,
    execute_code,
    verify_code_execution,
)
from app.tools.schemas import validate_tool_schema
from app.tools.types import ToolExecutionContext


def _context(**overrides) -> ToolExecutionContext:  # noqa: ANN003
    base = {
        "tenant_id": "tenant",
        "user_id": "user",
        "thread_id": "thread",
        "run_id": "run",
        "request_id": "request",
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


class FakeSandbox:
    """记录调用参数的沙箱替身。"""

    def __init__(
        self,
        *,
        result: SandboxResult | None = None,
        error: Exception | None = None,
        record: SandboxResult | None = None,
        record_error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._record = record
        self._record_error = record_error
        self.calls: list[dict] = []
        self.reads: list[tuple[str, str | None]] = []

    async def execute(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result

    async def get_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str | None = None,
    ):  # noqa: ANN201
        # 回读必须带租户，否则沙箱无法判断这条记录属于谁。
        self.reads.append((execution_id, tenant_id))
        if self._record_error is not None:
            raise self._record_error
        return self._record


@pytest.fixture
def sandbox_configured(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_base_url", "http://sandbox.test")
    monkeypatch.setattr(settings, "sandbox_api_key", "test-key")


@pytest.fixture
def sandbox_off(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_base_url", "")
    monkeypatch.setattr(settings, "sandbox_api_key", "")


def test_not_registered_without_base_url(sandbox_off):
    """没有沙箱地址时不应该存在这个工具，模型也就不该被告知有这能力。"""
    assert build_sandbox_tools() == []


def test_registered_when_configured(sandbox_configured):
    tools = build_sandbox_tools()

    assert len(tools) == 1

    tool = tools[0]

    assert tool.name == "execute_code"
    assert tool.kind == "http"
    assert tool.risk_level is RiskLevel.EXECUTE
    assert tool.requires_approval is True
    assert tool.enabled is True
    # 必须声明 verifier：没有回读，审批状态只能停在 EXECUTED。
    assert tool.verifier is not None
    # 网关硬超时必须大于沙箱预算，否则网关先超时，结果语义会失真。
    assert tool.timeout_seconds > settings.sandbox_timeout_seconds
    validate_tool_schema(tool.input_schema)


def test_schema_has_no_internal_details(sandbox_configured):
    """暴露给模型的 schema 里不能出现地址、密钥等实现细节。"""
    tool = build_sandbox_tools()[0]
    serialized = str(tool.input_schema)

    assert "sandbox.test" not in serialized
    assert "test-key" not in serialized


async def test_rejects_language_outside_allowlist(sandbox_configured, monkeypatch):
    fake = FakeSandbox(result=None)
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(_context(), {"language": "ruby", "code": "puts 1"})

    assert result.success is False
    assert result.error_code == "INVALID_ARGUMENTS"
    assert fake.calls == []


async def test_rejects_oversized_code(sandbox_configured, monkeypatch):
    fake = FakeSandbox(result=None)
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)
    monkeypatch.setattr(settings, "sandbox_max_code_bytes", 10)

    result = await execute_code(
        _context(),
        {"language": "python", "code": "print('way too long')"},
    )

    assert result.success is False
    assert result.error_code == "INVALID_ARGUMENTS"
    assert fake.calls == []


async def test_success_is_marked_verified(sandbox_configured, monkeypatch):
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-9",
            status="succeeded",
            exit_code=0,
            stdout="42\n",
            stderr="",
            duration_ms=8,
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(
        _context(idempotency_key="approval:thread:run"),
        {"language": "python", "code": "print(42)"},
    )

    assert result.success is True
    assert result.verified is True
    assert result.external_operation_id == "exec-9"
    assert result.result["stdout"] == "42\n"
    # 幂等键必须透传，否则连点两次确认会把同一段代码跑两遍。
    assert fake.calls[0]["idempotency_key"] == "approval:thread:run"


async def test_nonzero_exit_keeps_output(sandbox_configured, monkeypatch):
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-10",
            status="failed",
            exit_code=1,
            stdout="",
            stderr="ZeroDivisionError",
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(
        _context(),
        {"language": "python", "code": "1/0"},
    )

    assert result.success is False
    assert result.error_code == "NONZERO_EXIT"
    assert result.result["stderr"] == "ZeroDivisionError"
    assert result.retryable is False


async def test_timeout_tells_caller_not_to_retry(sandbox_configured, monkeypatch):
    fake = FakeSandbox(error=SandboxTimeout("沙箱执行超时"))
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(
        _context(),
        {"language": "python", "code": "while True: pass"},
    )

    assert result.success is False
    assert result.error_code == "SANDBOX_TIMEOUT"
    assert result.retryable is False
    assert "重试" in (result.message or "")


async def test_rejection_is_not_retryable(sandbox_configured, monkeypatch):
    fake = FakeSandbox(error=SandboxRejected("沙箱拒绝执行（HTTP 429）：配额不足"))
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(_context(), {"language": "python", "code": "print(1)"})

    assert result.success is False
    assert result.error_code == "SANDBOX_REJECTED"
    assert result.retryable is False


async def test_unavailable_does_not_claim_failure(sandbox_configured, monkeypatch):
    fake = FakeSandbox(error=SandboxUnavailable("无法连接代码沙箱服务"))
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(_context(), {"language": "python", "code": "print(1)"})

    assert result.success is False
    assert result.error_code == "SANDBOX_UNAVAILABLE"
    assert "无法确认" in (result.message or "")


async def test_timeout_budget_is_clamped(sandbox_configured, monkeypatch):
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-11",
            status="succeeded",
            exit_code=0,
            stdout="",
            stderr="",
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)
    monkeypatch.setattr(settings, "sandbox_timeout_seconds", 30)

    await execute_code(
        _context(),
        {"language": "python", "code": "print(1)", "timeout_seconds": 9999},
    )

    assert fake.calls[0]["timeout_seconds"] == 30


async def test_verifier_confirms_record(sandbox_configured, monkeypatch):
    """核验必须是回读，而不是相信执行时的返回值。"""
    fake = FakeSandbox(
        record=SandboxResult(
            execution_id="exec-v",
            status="succeeded",
            exit_code=0,
            stdout="5050\n",
            stderr="",
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await verify_code_execution(
        _context(external_operation_id="exec-v"),
        {"language": "python"},
    )

    assert fake.reads == [("exec-v", "tenant")]
    assert result.verified is True
    assert result.success is True
    assert result.verification["reason"] == "sandbox_record_readback"
    assert result.result["stdout"] == "5050\n"


async def test_verifier_needs_execution_id(sandbox_configured, monkeypatch):
    fake = FakeSandbox(record=None)
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await verify_code_execution(_context(), {"language": "python"})

    assert result.verified is False
    assert result.error_code == "MISSING_EXECUTION_ID"
    assert fake.reads == []


async def test_verifier_reports_missing_record(sandbox_configured, monkeypatch):
    """沙箱重启或记录过期时，不能假装已核验。"""
    fake = FakeSandbox(record_error=SandboxRejected("404 not found"))
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await verify_code_execution(
        _context(external_operation_id="exec-lost"),
        {"language": "python"},
    )

    assert result.verified is False
    assert result.error_code == "EXECUTION_RECORD_MISSING"


async def test_verifier_marks_failed_run_as_verified(sandbox_configured, monkeypatch):
    """“已核验”说的是记录对得上，不代表代码成功。两件事必须分开。"""
    fake = FakeSandbox(
        record=SandboxResult(
            execution_id="exec-bad",
            status="failed",
            exit_code=1,
            stdout="",
            stderr="ZeroDivisionError",
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await verify_code_execution(
        _context(external_operation_id="exec-bad"),
        {"language": "python"},
    )

    assert result.verified is True
    assert result.success is False
    assert result.result["stderr"] == "ZeroDivisionError"


# ---------------- 策略快照推送 ----------------


async def test_policy_snapshot_is_pushed_with_every_execution(
    sandbox_configured, monkeypatch
):
    """每次执行都把**自包含**的一份策略推过去。

    沙箱不该在执行中途回头问平台「现在是什么策略」——那次读取与这次提交
    之间管理员可能刚按了开关，而代码已经按旧规则跑起来了。
    """
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-20",
            status="succeeded",
            exit_code=0,
            stdout="ok",
            stderr="",
            policy_revision=0,
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    await execute_code(_context(), {"language": "python", "code": "print(1)"})

    call = fake.calls[0]
    assert call["policy_revision"] == 0
    # 推的是点分键的完整值，不是「你自己去查」。
    assert call["policy"]["permissions.default_action"] == "allow"
    assert "tool.backup_threshold_mb" in call["policy"]


async def test_a_different_revision_coming_back_is_flagged(
    sandbox_configured, monkeypatch
):
    """沙箱回报另一个版本 = 它按别的规则跑的。

    这时不能把结果当成这次调用的结论：数字可能对，但「按什么规则算出来的」
    答不上来。原始输出仍然保留，人要能看到它到底干了什么。
    """
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-21",
            status="succeeded",
            exit_code=0,
            stdout="42",
            stderr="",
            policy_revision=3,
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(_context(), {"language": "python", "code": "print(42)"})

    assert result.success is False
    assert result.error_code == "POLICY_SNAPSHOT_MISMATCH"
    assert result.result["stdout"] == "42"
    assert "第 3 版" in (result.message or "")


async def test_a_missing_echo_is_not_treated_as_a_mismatch(
    sandbox_configured, monkeypatch
):
    """老版本沙箱不回这个字段。缺字段说明协议还没实现，不是它用了别的版本——
    把它判成失败，升级平台就会让所有执行一夜之间变红。
    """
    fake = FakeSandbox(
        result=SandboxResult(
            execution_id="exec-22",
            status="succeeded",
            exit_code=0,
            stdout="42",
            stderr="",
            policy_revision=None,
        )
    )
    monkeypatch.setattr(sandbox_tools, "_resolve_client", lambda: fake)

    result = await execute_code(_context(), {"language": "python", "code": "print(42)"})

    assert result.success is True
    assert result.error_code is None
