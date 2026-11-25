# tests/unit/test_task_models.py
"""事务层模型：租户隔离、NULL 语义、trace 追加与 state 更新。

这几处是 P0 服务层会依赖的契约：
- 四张表都挂 TenantScoped，漏 where 也读不到别的租户；
- cpu_ms 为 NULL 是「没测到」，不是 0；
- trace 按 seq 追加，同键重复插入直接冲突（不能覆盖事实日志）；
- state 同 execution_id 唯一，更新的是当前态那一行。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import async_session_factory
from app.models.task import (
    ExecutionState,
    ExecutionStatus,
    ExecutionTrace,
    Task,
    TaskExecution,
    TaskKind,
    TaskStatus,
    Workspace,
)
from app.tenancy import TenantContextMissingError, tenant_scope


def _workspace(tenant: str, user: str = "u1") -> Workspace:
    return Workspace(
        tenant_id=tenant,
        user_id=user,
        root_path=f"/data/workspaces/{tenant}/{user}",
    )


def _task(tenant: str, workspace_id: uuid.UUID, key: str) -> Task:
    return Task(
        tenant_id=tenant,
        user_id="u1",
        workspace_id=workspace_id,
        kind=TaskKind.CODE,
        status=TaskStatus.QUEUED,
        idempotency_key=key,
    )


def _execution(tenant: str, task_id: uuid.UUID, execution_id: str) -> TaskExecution:
    return TaskExecution(
        tenant_id=tenant,
        execution_id=execution_id,
        task_id=task_id,
        user_id="u1",
        status=ExecutionStatus.SUCCEEDED,
    )


async def test_workspace_is_tenant_scoped():
    async with async_session_factory() as session:
        with tenant_scope("tw-a"):
            session.add(_workspace("tw-a"))
            await session.commit()

    async with async_session_factory() as session:
        with tenant_scope("tw-a"):
            assert len((await session.scalars(select(Workspace))).all()) == 1
        with tenant_scope("tw-b"):
            assert (await session.scalars(select(Workspace))).all() == []


async def test_cpu_ms_none_is_not_zero():
    async with async_session_factory() as session:
        with tenant_scope("tw-null"):
            ws = _workspace("tw-null")
            session.add(ws)
            await session.flush()
            task = _task("tw-null", ws.id, "tw-null:no-metrics")
            session.add(task)
            await session.flush()
            missing = _execution("tw-null", task.id, "exec-no-metrics")
            measured = _execution("tw-null", task.id, "exec-zero")
            measured.cpu_ms = 0
            session.add_all([missing, measured])
            await session.commit()

            rows = {
                row.execution_id: row.cpu_ms
                for row in (await session.scalars(select(TaskExecution))).all()
            }

    assert rows["exec-no-metrics"] is None
    assert rows["exec-zero"] == 0


async def test_workspace_unique_per_tenant_user():
    async with async_session_factory() as session:
        with tenant_scope("tw-dup"):
            session.add(_workspace("tw-dup"))
            await session.commit()

    async with async_session_factory() as session:
        with tenant_scope("tw-dup"):
            session.add(_workspace("tw-dup"))
            with pytest.raises(IntegrityError):
                await session.commit()
        await session.rollback()


async def test_write_without_context_is_rejected():
    """显式传 tenant_id 也不能替代上下文：不进 tenant_scope 就写不进去。"""
    async with async_session_factory() as session:
        session.add(
            Workspace(
                user_id="u1",
                root_path="/data/workspaces/no-context",
            )
        )

        with pytest.raises(TenantContextMissingError):
            await session.commit()


async def test_trace_is_append_only_by_seq():
    async with async_session_factory() as session:
        with tenant_scope("tw-trace"):
            for seq, to_status in ((0, "queued"), (1, "running"), (2, "succeeded")):
                session.add(
                    ExecutionTrace(
                        tenant_id="tw-trace",
                        execution_id="exec-trace",
                        seq=seq,
                        from_status=None if seq == 0 else ("queued" if seq == 1 else "running"),
                        to_status=to_status,
                        actor="sandbox",
                    )
                )
            await session.commit()

            rows = (
                await session.scalars(
                    select(ExecutionTrace)
                    .where(ExecutionTrace.execution_id == "exec-trace")
                    .order_by(ExecutionTrace.seq)
                )
            ).all()
            assert [row.to_status for row in rows] == ["queued", "running", "succeeded"]

            session.add(
                ExecutionTrace(
                    tenant_id="tw-trace",
                    execution_id="exec-trace",
                    seq=1,
                    from_status="queued",
                    to_status="cancelled",
                    actor="platform",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        await session.rollback()


async def test_state_is_one_per_execution_and_updatable():
    async with async_session_factory() as session:
        with tenant_scope("tw-state"):
            session.add(
                ExecutionState(
                    tenant_id="tw-state",
                    execution_id="exec-state",
                    status="running",
                    state_json={"phase": "start"},
                )
            )
            await session.commit()

            state = (
                await session.scalars(
                    select(ExecutionState).where(
                        ExecutionState.execution_id == "exec-state"
                    )
                )
            ).one()
            state.status = "succeeded"
            state.state_json = {"phase": "done", "exit_code": 0}
            await session.commit()

            rows = (
                await session.scalars(
                    select(ExecutionState).where(
                        ExecutionState.execution_id == "exec-state"
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].status == "succeeded"
            assert rows[0].state_json["exit_code"] == 0
