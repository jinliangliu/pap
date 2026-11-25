# app/tools/sandbox_tools.py
"""代码执行工具：把代码委派给外部沙箱。

为什么不在 API 进程里 `exec()`：

- 进程内执行没有隔离边界，一段 `import os; os.remove(...)` 就能删掉服务文件；
- 无法限制 CPU、内存、网络和文件系统；
- 超时只能靠信号，且会污染 asyncio 事件循环。

因此这里只做三件事：参数校验、调用沙箱、把结果翻译成平台的结果语义。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.integrations.sandbox_client import (
    SandboxClient,
    SandboxError,
    SandboxRejected,
    SandboxResult,
    SandboxTimeout,
    SandboxUnavailable,
    get_sandbox_client,
)
from app.models.approval import RiskLevel
from app.services import runtime_settings
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

# stdout/stderr 之外的固定说明，避免模型把截图式的输出当成完整结果。
TRUNCATION_NOTE = "输出已按平台上限截断，只保留了前面部分。"


def _resolve_client() -> SandboxClient:
    return get_sandbox_client()


def _validate(*, code: str, language: str) -> str | None:
    """返回错误说明；None 表示通过。"""
    if not code.strip():
        return "代码内容为空。"

    if len(code.encode("utf-8")) > settings.sandbox_max_code_bytes:
        return f"代码大小超过 {settings.sandbox_max_code_bytes} 字节上限，请拆分后再提交。"

    allowed = settings.sandbox_allowed_languages

    if allowed and language not in allowed:
        return f"不允许的语言 {language!r}，可选：{', '.join(allowed)}。"

    return None


async def execute_code(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    language = str(arguments["language"])
    code = str(arguments["code"])
    stdin = str(arguments.get("stdin") or "")

    invalid = _validate(code=code, language=language)

    if invalid is not None:
        return ToolExecutionResult(
            success=False,
            message=invalid,
            error_code="INVALID_ARGUMENTS",
            retryable=False,
        )

    # 用户传的超时不能超过平台预算，否则沙箱侧和网关侧会出现两个不同的上限。
    requested = arguments.get("timeout_seconds")
    budget = settings.sandbox_timeout_seconds

    if isinstance(requested, int) and not isinstance(requested, bool):
        budget = max(1, min(requested, settings.sandbox_timeout_seconds))

    # 钉住的版本优先；没钉就取当前。**两者都必须带上**：沙箱那段代码的
    # 行为（能不能写、写多大、要不要留档）取决于这些值，而它不该在执行中途
    # 回头问平台一次——那次读取与这次提交之间，管理员可能刚按了开关。
    policy_revision = context.policy_revision or runtime_settings.current_revision()
    policy = await runtime_settings.snapshot_at(policy_revision) or {}

    try:
        result = await _resolve_client().execute(
            code=code,
            language=language,
            stdin=stdin,
            timeout_seconds=budget,
            idempotency_key=context.idempotency_key,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id or None,
            policy_revision=policy_revision,
            policy=policy,
        )
    except SandboxRejected as exc:
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="SANDBOX_REJECTED",
            retryable=False,
        )
    except SandboxTimeout as exc:
        # 结果未知：不能声称失败，也不能自动重发。
        return ToolExecutionResult(
            success=False,
            message=f"{exc} 请勿直接重试，先确认沙箱侧是否已有该次执行。",
            error_code="SANDBOX_TIMEOUT",
            retryable=False,
        )
    except SandboxUnavailable as exc:
        return ToolExecutionResult(
            success=False,
            message=f"{exc} 本次是否已开始执行无法确认。",
            error_code="SANDBOX_UNAVAILABLE",
            retryable=False,
        )
    except SandboxError as exc:
        logger.warning("沙箱调用失败：%s", exc)
        return ToolExecutionResult(
            success=False,
            message=str(exc),
            error_code="SANDBOX_ERROR",
            retryable=False,
        )

    payload = _to_payload(language, result)
    payload["policy_revision"] = policy_revision

    # 回显校验：沙箱回报的版本与推过去的不是同一个，说明它按另一套配置跑的。
    #
    # 这里**不**把代码执行算成成功：结果可能对，但「按哪套规则跑出来的」
    # 答不上来，而一次说不清依据的执行不该被当成这次调用的结论。原始输出
    # 仍然完整保留，人能看到它到底干了什么。
    #
    # 老版本沙箱不回这个字段（None），不判失败——协议是新增的，缺字段说明
    # 对方还没实现，不是它用了别的版本。
    if result.policy_revision is not None and result.policy_revision != policy_revision:
        return ToolExecutionResult(
            success=False,
            verified=False,
            external_operation_id=result.execution_id,
            result=payload,
            message=(
                f"沙箱按策略第 {result.policy_revision} 版执行，而本次提交的是第 "
                f"{policy_revision} 版。执行结果不能按本次策略解释，已保留原始输出。"
            ),
            error_code="POLICY_SNAPSHOT_MISMATCH",
            retryable=False,
        )

    if result.succeeded:
        return ToolExecutionResult(
            success=True,
            verified=True,
            external_operation_id=result.execution_id,
            result=payload,
            message=(
                "代码已在沙箱中执行完毕。"
                if not result.truncated
                else f"代码已在沙箱中执行完毕。{TRUNCATION_NOTE}"
            ),
            verification={
                "verified": True,
                "reason": "execution_output_observed",
            },
        )

    # 代码跑完了但退出码非 0：这是业务失败，不是平台故障，原始输出要完整保留。
    return ToolExecutionResult(
        success=False,
        verified=True,
        external_operation_id=result.execution_id,
        result=payload,
        message=(f"代码执行结束但未成功（status={result.status}，exit_code={result.exit_code}）。"),
        verification={
            "verified": True,
            "reason": "execution_output_observed",
        },
        error_code="NONZERO_EXIT",
        retryable=False,
    )


def _to_payload(language: str, result: SandboxResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "language": language,
        "status": result.status,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "cpu_ms": result.cpu_ms,
        "truncated": result.truncated,
        "backend": result.backend,
    }

    if result.execution_id:
        payload["execution_id"] = result.execution_id

    return payload


async def verify_code_execution(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """
    回读沙箱执行记录。

    为什么必须回读：审批服务只在拿到独立证据时才把状态推进到 VERIFIED。
    执行时的返回值只能证明"我们拿到了一个响应"，不能证明"沙箱侧留下了这条记录"。
    两者都成立，才算真的完成。
    """
    execution_id = context.external_operation_id

    if not execution_id:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message="缺少执行 ID，无法回读核验。",
            error_code="MISSING_EXECUTION_ID",
        )

    try:
        record = await _resolve_client().get_execution(
            execution_id,
            tenant_id=context.tenant_id,
        )
    except SandboxRejected as exc:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message=f"沙箱中查不到该次执行记录，无法核验：{exc}",
            error_code="EXECUTION_RECORD_MISSING",
        )
    except SandboxError as exc:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message=f"回读沙箱执行记录失败：{exc}",
            error_code="SANDBOX_ERROR",
        )

    language = str(arguments.get("language") or "unknown")

    return ToolExecutionResult(
        success=record.succeeded,
        verified=True,
        external_operation_id=execution_id,
        result=_to_payload(language, record),
        message="已回读沙箱执行记录，结果与提交时一致。",
        verification={
            "verified": True,
            "reason": "sandbox_record_readback",
            "status": record.status,
            "exit_code": record.exit_code,
        },
    )


def build_sandbox_tools() -> list[ToolDefinition]:
    """
    未配置沙箱地址时返回空列表。

    这比“注册一个一定失败的工具”更安全：模型看不到该能力，
    也就不会在回答里承诺自己能跑代码。
    """
    if not settings.sandbox_enabled:
        return []

    max_timeout = settings.sandbox_timeout_seconds

    return [
        ToolDefinition(
            name="execute_code",
            display_name="执行代码",
            description=(
                "在隔离沙箱中执行一段代码，返回 stdout、stderr 与退出码。"
                "适用于计算、数据批处理、结果验证等需要真实运行的场景。"
                "代码中不要写入任何密钥；需要输入数据时用 stdin 传入。"
            ),
            kind="http",
            risk_level=RiskLevel.EXECUTE,
            input_schema={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": list(settings.sandbox_allowed_languages),
                        "description": "执行代码所用语言。",
                    },
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "description": "要执行的完整代码。",
                    },
                    "stdin": {
                        "type": "string",
                        "description": "标准输入内容，可选。",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": max_timeout,
                        "description": "执行超时，不能超过平台预算。",
                    },
                },
                "required": ["language", "code"],
                "additionalProperties": False,
            },
            handler=execute_code,
            enabled=True,
            # 网关的硬超时要大于沙箱预算，留出提交与轮询的余量，
            # 否则网关会先于沙箱判定超时，状态语义就乱了。
            timeout_seconds=max_timeout + 5,
            requires_approval=settings.sandbox_requires_approval,
            verifier=verify_code_execution,
            verification_hint="确认后回读沙箱执行记录，确认该次执行确实已落库并取到退出码。",
        )
    ]
