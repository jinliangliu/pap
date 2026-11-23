# tests/unit/test_tool_decision_audit.py
"""审计按「决策」记录，而不是按「命令跑成功」记录。

原先只有执行完成那一条：进程在 handler 里被杀掉、或者备份失败提前抛异常时，
审计里什么都不留。而「批过了、要开始做、然后没了」恰恰是最需要事后看见的
一种状态——它既不是成功，也不是拒绝。

这套用例守三件事：

1. 放行先落一条，执行再落一条，顺序不能反；
2. 决策行带着依据（哪一版策略、备份规则）；
3. 备份失败时**决策仍然在**，写入却没有发生。
"""

from __future__ import annotations

import pytest

from app.models.approval import RiskLevel
from app.tools import gateway as gateway_module
from app.tools.gateway import (
    DECISION_ALLOW,
    DECISION_ALLOW_WITH_BACKUP,
    DECISION_APPROVED,
    ToolBackupFailedError,
    ToolGateway,
)
from app.tools.registry import ToolRegistry
from app.tools.types import (
    BackupSpec,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)
from tests.conftest import TEST_SUBJECT, TEST_TENANT


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def of(self, event: str) -> list[dict]:
        return [payload for name, payload in self.events if name == event]


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=TEST_TENANT,
        user_id=TEST_SUBJECT,
        thread_id="th1",
        run_id="run1",
        request_id="req1",
        scopes=("tools:read",),
    )


def _read_tool(calls: list[str]) -> ToolDefinition:
    async def handler(ctx, arguments):  # noqa: ANN001
        calls.append("read")
        return ToolExecutionResult(success=True, result={"ok": True})

    return ToolDefinition(
        name="look",
        display_name="看",
        description="只读。",
        kind="python",
        risk_level=RiskLevel.READ,
        required_scopes=("tools:read",),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


def _write_tool(calls: list[str], *, backup=None) -> ToolDefinition:
    async def handler(ctx, arguments):  # noqa: ANN001
        calls.append("write")
        return ToolExecutionResult(success=True, result={"written": True})

    return ToolDefinition(
        name="save",
        display_name="保存",
        description="写入。",
        kind="python",
        risk_level=RiskLevel.WRITE,
        required_scopes=("tools:read",),
        requires_approval=True,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        backup=backup,
    )


def _gateway(tool: ToolDefinition, sink: _Sink) -> ToolGateway:
    registry = ToolRegistry()
    gateway = ToolGateway(registry, audit_sink=sink)
    gateway.register_tool(tool)
    return gateway


async def test_decision_lands_before_the_result(seeded_identity):
    calls: list[str] = []
    sink = _Sink()

    await _gateway(_read_tool(calls), sink).execute_read_only(
        context=_context(), tool_name="look", arguments={}
    )

    assert [name for name, _ in sink.events] == ["tool_allowed", "tool_executed"]
    assert calls == ["read"]

    decision = sink.of("tool_allowed")[0]
    assert decision["decision"] == DECISION_ALLOW
    assert decision["success"] is True
    assert "policy_revision" in decision


async def test_backup_hook_turns_the_decision_into_allow_with_backup(seeded_identity):
    calls: list[str] = []
    sink = _Sink()

    async def hook(ctx, arguments):  # noqa: ANN001
        return BackupSpec(
            resource_type="workflow",
            resource_id="wf-1",
            payload={"definition": {"entry": "start", "nodes": []}},
        )

    await _gateway(_write_tool(calls, backup=hook), sink).execute_approved(
        context=_context(), tool_name="save", arguments={}
    )

    decision = sink.of("tool_allowed")[0]
    outcome = sink.of("tool_executed")[0]

    assert decision["decision"] == DECISION_ALLOW_WITH_BACKUP
    # 决策行在备份之前写，所以它只说规则，不说这次落成了没有。
    assert "backup_captured" not in decision
    assert decision["backup_enabled"] is True
    assert decision["backup_threshold_mb"] == 100
    assert decision["backup_max_size_mb"] == 3000
    # 钩子有没有声明，是「读到 captured 为空时该做什么」的唯一依据。
    assert decision["backup_hook"] is True

    assert outcome["decision"] == DECISION_ALLOW_WITH_BACKUP
    assert outcome["backup_captured"] is True
    # 原件落在哪一行，只能由结果事件给出：备份在决策之后发生。
    # 少了它，事后要恢复的人只能拿时间戳去表里碰运气。
    assert outcome["backup_id"]
    assert outcome["backup_size_bytes"] > 0


async def test_without_a_hook_the_decision_stays_approved(seeded_identity):
    calls: list[str] = []
    sink = _Sink()

    await _gateway(_write_tool(calls), sink).execute_approved(
        context=_context(), tool_name="save", arguments={}
    )

    assert sink.of("tool_allowed")[0]["decision"] == DECISION_APPROVED
    # 没声明钩子要能一眼看出：否则读审计的人会以为「这次本该备份但没备」。
    assert sink.of("tool_allowed")[0]["backup_hook"] is False
    assert sink.of("tool_executed")[0]["backup_hook"] is False
    assert "backup_captured" not in sink.of("tool_executed")[0]


async def test_backup_failure_keeps_the_decision_and_blocks_the_write(seeded_identity):
    """备份失败时，写入没发生——但「它被批准过」这条必须留下。

    这正是「按决策记」与「按成功记」的分界：只记结果的话，这一次调用在
    审计里会完全消失，而它其实是「批了、没做成」。
    """
    calls: list[str] = []
    sink = _Sink()

    async def broken(ctx, arguments):  # noqa: ANN001
        raise RuntimeError("备份系统不可用")

    with pytest.raises(ToolBackupFailedError):
        await _gateway(_write_tool(calls, backup=broken), sink).execute_approved(
            context=_context(), tool_name="save", arguments={}
        )

    assert calls == []
    assert [name for name, _ in sink.events] == ["tool_allowed"]
    assert sink.of("tool_allowed")[0]["decision"] == DECISION_ALLOW_WITH_BACKUP


async def test_switch_off_falls_back_to_plain_approval(seeded_identity, monkeypatch):
    """开关关着时决策不该写成 allow_with_backup——那会让人以为留了档。"""
    calls: list[str] = []
    sink = _Sink()

    monkeypatch.setattr(gateway_module.runtime_settings, "backup_enabled", lambda: False)

    async def hook(ctx, arguments):  # noqa: ANN001
        return BackupSpec(resource_type="workflow", resource_id="wf-1", payload={})

    await _gateway(_write_tool(calls, backup=hook), sink).execute_approved(
        context=_context(), tool_name="save", arguments={}
    )

    assert sink.of("tool_allowed")[0]["decision"] == DECISION_APPROVED
    assert calls == ["write"]


# ---------------- 落库那一侧 ----------------


async def test_sink_writes_a_decision_row_with_its_evidence(seeded_identity):
    """审计落库：事件类型、人话、以及「依据哪一版策略」。

    `DbAuditSink` 的映射表是这些字段唯一的落点——写错一个键名不会报错，
    只会静默退回 `TOOL_REQUESTED`，于是审计页上「被允许的调用」看起来
    和「只是提了一下」一模一样。
    """
    from sqlalchemy import delete, select

    from app.db.session import async_session_factory
    from app.models.audit import AuditEventType, AuditLog
    from app.services.audit_service import DbAuditSink

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(AuditLog))

    await DbAuditSink().record(
        "tool_allowed",
        {
            "tool_name": "save",
            "risk_level": "write",
            "decision": DECISION_ALLOW_WITH_BACKUP,
            "policy_revision": 4,
            "success": True,
            "argument_keys": ["definition"],
            "backup_enabled": True,
            "backup_threshold_mb": 100,
            "backup_max_size_mb": 3000,
            "tenant_id": TEST_TENANT,
            "user_id": TEST_SUBJECT,
            "thread_id": "th1",
            "run_id": "run1",
            "agent_name": "构建助手",
        },
    )

    async with async_session_factory() as session:
        row = await session.scalar(
            select(AuditLog).where(AuditLog.event_type == AuditEventType.TOOL_ALLOWED)
        )

    assert row is not None
    assert row.metadata_json["decision"] == DECISION_ALLOW_WITH_BACKUP
    assert row.metadata_json["policy_revision"] == 4
    assert row.metadata_json["backup_threshold_mb"] == 100
    # 人话列必须说「获准」而不是「已执行」：后面那条才是结果。
    assert "获准调用" in (row.message or "")
