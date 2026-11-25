# tests/integration/test_schedule_delivery.py
"""定时任务投递链路：授权快照、closed set、当前 RBAC 权限、产物不受投递影响。"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.integrations.sandbox_client import SandboxResult
from app.models.artifact import Artifact
from app.models.rbac import Role, RolePermission, User, UserRole
from app.models.task import Schedule, Task, TaskKind, TaskStatus
from app.services import snapshot_service, workspace_service
from app.services import task_runner_service as runner
from app.services.delivery_service import authorize_delivery, revoke_delivery
from app.services.task_service import create_task
from app.tenancy import tenant_scope
from app.tools.types import ToolExecutionResult

_TENANT = "ti-delivery"
_TOOL = "mock.delivery.tool"


@dataclass
class FakeSandbox:
    stdout: str

    async def execute(self, **kwargs) -> SandboxResult:
        return SandboxResult(
            execution_id="exec-delivery-1",
            status="succeeded",
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            cpu_ms=11,
        )

    async def get_execution(self, execution_id: str, *, tenant_id: str) -> SandboxResult:
        return await self.execute()


@dataclass
class FakeGateway:
    result: ToolExecutionResult | None = None
    error: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    async def execute_approved(self, *, context, tool_name, arguments):
        self.calls.append(
            {
                "context": context,
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        if self.error:
            raise self.error
        return self.result or ToolExecutionResult(
            success=True,
            message="已投递。",
            external_operation_id="ext-1",
        )


def _docx_stdout() -> str:
    content = b"PK\x03\x04" + (b"delivery" * 200)
    return "ok\nPAP_ARTIFACT:" + base64.b64encode(content).decode("ascii") + "\n"


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workspace_service.settings, "workspace_storage_dir", str(tmp_path / "ws")
    )
    monkeypatch.setattr(
        snapshot_service.settings, "workspace_storage_dir", str(tmp_path / "ws")
    )
    return tmp_path / "ws"


@pytest.fixture
async def seeded(workspace_dir):
    """建租户成员 + 工作区 + 已授权的 schedule，返回 (user_id, schedule_id, workspace_id)。"""
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.external_subject == "delivery-user")
                )
                if user is None:
                    user = User(
                        external_subject="delivery-user",
                        display_name="delivery-user",
                        enabled=True,
                    )
                    session.add(user)
                    await session.flush()

                role = await session.scalar(select(Role).where(Role.name == "delivery-test"))
                if role is None:
                    role = Role(name="delivery-test", description="delivery-test")
                    session.add(role)
                    await session.flush()
                    session.add(
                        RolePermission(role_id=role.id, permission="tools:read")
                    )
                    await session.flush()

                # 每个用例重建绑定：有一个用例会故意撤权，不能让它影响后面。
                stale = await session.scalars(
                    select(UserRole).where(UserRole.user_id == user.id)
                )
                for row in stale:
                    await session.delete(row)
                await session.flush()
                session.add(
                    UserRole(tenant_id=_TENANT, user_id=user.id, role_id=role.id)
                )
                await session.flush()

                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id=_TENANT, user_id=str(user.id)
                )
                schedule = Schedule(
                    tenant_id=_TENANT,
                    user_id=str(user.id),
                    workspace_id=ws.id,
                    name="每日汇总",
                    kind=TaskKind.SCHEDULE,
                    cron="0 8 * * *",
                    timezone_offset_hours=8,
                    next_fire_at=datetime.now(UTC) + timedelta(days=1),
                )
                session.add(schedule)
                await session.flush()
                await authorize_delivery(
                    session,
                    schedule_id=schedule.id,
                    tool_name=_TOOL,
                    parameter_bounds={"to": ["a@example.com"], "body": "*"},
                    created_by=str(user.id),
                )
                return str(user.id), schedule.id, ws.id


async def _new_schedule_task(user_id: str, workspace_id, schedule_id, arguments: dict):
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                schedule = await session.get(Schedule, schedule_id)
                task, _ = await create_task(
                    session,
                    tenant_id=_TENANT,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    kind=TaskKind.SCHEDULE,
                    idempotency_key=f"test-schedule-{uuid.uuid4().hex}",
                    input_json={
                        "compute": {
                            "kind": "document",
                            "title": "周报",
                            "markdown": "# 周报\n完成 X",
                        },
                        "delivery": {"tool_name": _TOOL, "arguments": arguments},
                    },
                    delivery_grant_json=dict(schedule.delivery_grant_json),
                    schedule_id=schedule_id,
                )
                return task.id


async def _load(task_id):
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            artifacts = list(
                await session.scalars(select(Artifact).where(Artifact.user_id == task.user_id))
            )
            return task, artifacts


async def test_schedule_task_delivers_with_current_scopes(seeded):
    user_id, schedule_id, workspace_id = seeded
    task_id = await _new_schedule_task(
        user_id,
        workspace_id,
        schedule_id,
        {"to": ["a@example.com"], "body": "本周完成 X"},
    )
    gateway = FakeGateway()
    with tenant_scope(_TENANT):
        state = await runner.run_task(
            task_id=task_id,
            client=FakeSandbox(_docx_stdout()),
            gateway=gateway,
        )

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts = await _load(task_id)
    assert task.output_json["delivery"]["status"] == "delivered"
    assert task.output_json["delivery"]["external_operation_id"] == "ext-1"
    assert artifacts  # 计算产物仍在，投递独立记账
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["tool_name"] == _TOOL
    assert call["context"].scopes == ("tools:read",)
    assert call["context"].is_platform_admin is False
    assert call["context"].idempotency_key == f"delivery:{task_id}"


async def test_out_of_bound_delivery_denied_but_artifact_kept(seeded):
    user_id, schedule_id, workspace_id = seeded
    task_id = await _new_schedule_task(
        user_id,
        workspace_id,
        schedule_id,
        {"to": ["a@example.com"], "body": "hi", "bcc": "evil@example.com"},
    )
    gateway = FakeGateway()
    with tenant_scope(_TENANT):
        state = await runner.run_task(
            task_id=task_id,
            client=FakeSandbox(_docx_stdout()),
            gateway=gateway,
        )

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts = await _load(task_id)
    delivery = task.output_json["delivery"]
    assert delivery["status"] == "denied"
    assert delivery["error_code"] == "DELIVERY_NOT_AUTHORIZED"
    assert "bcc" in delivery["message"]
    assert "evil@example.com" not in delivery["message"]
    assert gateway.calls == []
    assert artifacts  # 投递越界不抹掉已生成的产物


async def test_scopes_are_resolved_each_time_not_snapshotted(seeded):
    user_id, schedule_id, workspace_id = seeded
    first = await _new_schedule_task(
        user_id, workspace_id, schedule_id, {"to": ["a@example.com"], "body": "1"}
    )
    gateway = FakeGateway()
    with tenant_scope(_TENANT):
        await runner.run_task(task_id=first, client=FakeSandbox(_docx_stdout()), gateway=gateway)
        assert gateway.calls[0]["context"].scopes == ("tools:read",)

        # 撤掉权限后再跑一次：授权快照没变，但上下文里的 scopes 必须为空。
        async with async_session_factory() as session:
            async with session.begin():
                member = await session.scalar(
                    select(User).where(User.external_subject == "delivery-user")
                )
                bindings = await session.scalars(
                    select(UserRole).where(UserRole.user_id == member.id)
                )
                for binding in bindings:
                    await session.delete(binding)

    second = await _new_schedule_task(
        user_id, workspace_id, schedule_id, {"to": ["a@example.com"], "body": "2"}
    )
    with tenant_scope(_TENANT):
        await runner.run_task(
            task_id=second, client=FakeSandbox(_docx_stdout()), gateway=gateway
        )
    assert gateway.calls[1]["context"].scopes == ()


async def test_authorize_and_revoke_persist(seeded):
    user_id, schedule_id, _ = seeded
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            row = await session.get(Schedule, schedule_id)
            assert row.delivery_grant_json["revoked_at"] is None
            assert row.delivery_grant_json["parameter_bounds"]["to"] == ["a@example.com"]

        async with async_session_factory() as session:
            async with session.begin():
                assert await revoke_delivery(
                    session, schedule_id=schedule_id, actor_id=user_id
                )

        async with async_session_factory() as session:
            row = await session.get(Schedule, schedule_id)
            assert row.delivery_grant_json["revoked_at"] is not None


async def test_failed_delivery_does_not_fail_computation(seeded):
    user_id, schedule_id, workspace_id = seeded
    task_id = await _new_schedule_task(
        user_id, workspace_id, schedule_id, {"to": ["a@example.com"], "body": "hi"}
    )
    gateway = FakeGateway(
        result=ToolExecutionResult(
            success=False, error_code="TOOL_TIMEOUT", message="外部系统超时。"
        )
    )
    with tenant_scope(_TENANT):
        state = await runner.run_task(
            task_id=task_id,
            client=FakeSandbox(_docx_stdout()),
            gateway=gateway,
        )

    assert state == TaskStatus.SUCCEEDED.value
    task, _ = await _load(task_id)
    assert task.output_json["delivery"]["status"] == "failed"
    assert task.output_json["delivery"]["error_code"] == "TOOL_TIMEOUT"


async def test_revoke_interrupts_queued_task_delivery(seeded):
    """撤销要打断已入队任务的投递，但不取消它的计算。"""
    user_id, schedule_id, workspace_id = seeded
    task_id = await _new_schedule_task(
        user_id,
        workspace_id,
        schedule_id,
        {"to": ["a@example.com"], "body": "还没跑"},
    )
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                assert await revoke_delivery(
                    session, schedule_id=schedule_id, actor_id=user_id
                )

    gateway = FakeGateway()
    with tenant_scope(_TENANT):
        state = await runner.run_task(
            task_id=task_id,
            client=FakeSandbox(_docx_stdout()),
            gateway=gateway,
        )

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts = await _load(task_id)
    delivery = task.output_json["delivery"]
    assert delivery["status"] == "denied"
    assert delivery["error_code"] == "DELIVERY_NOT_AUTHORIZED"
    assert "撤销" in delivery["message"]
    assert gateway.calls == []
    assert artifacts  # 撤销切断的是出网，不是计算


async def test_revoke_does_not_rewrite_completed_delivery(seeded):
    """已送达的任务不回改——撤销收不回已经发生的投递，改记录才是说谎。"""
    user_id, schedule_id, workspace_id = seeded
    task_id = await _new_schedule_task(
        user_id,
        workspace_id,
        schedule_id,
        {"to": ["a@example.com"], "body": "已送达"},
    )
    gateway = FakeGateway()
    with tenant_scope(_TENANT):
        await runner.run_task(
            task_id=task_id, client=FakeSandbox(_docx_stdout()), gateway=gateway
        )
        async with async_session_factory() as session:
            async with session.begin():
                await revoke_delivery(
                    session, schedule_id=schedule_id, actor_id=user_id
                )

    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            assert task.output_json["delivery"]["status"] == "delivered"
            assert task.delivery_grant_json["revoked_at"] is None
    assert len(gateway.calls) == 1
