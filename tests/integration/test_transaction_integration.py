# tests/integration/test_transaction_integration.py
"""事务层集成：工作区隔离、快照打与清理、任务幂等入队、定时触发上下文。

覆盖那些「只有真跑起来才发现错」的契约：
- 工作区路径由服务端生成且防穿越；同租户内按 user 隔离；
- 快照不套快照，清理按保留规则删并写审计；
- 任务幂等键挡重复，非法迁移抛错；
- 定时触发在独立租户 Context 里写库，不漏租户也不以 system 写。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.audit import AuditEventType, AuditLog
from app.models.task import (
    Schedule,
    Task,
    TaskKind,
    TaskStatus,
)
from app.services import schedule_service, snapshot_service, workspace_service
from app.services.task_service import (
    InvalidTransition,
    create_task,
    request_cancel,
    transition,
)
from app.tenancy import tenant_scope


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_service.settings, "workspace_storage_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(snapshot_service.settings, "workspace_storage_dir", str(tmp_path / "ws"))
    return tmp_path / "ws"


# ---- 工作区 ----------------------------------------------------------------

def test_build_root_path_rejects_traversal(workspace_dir):
    with pytest.raises(workspace_service.WorkspaceError):
        workspace_service.build_root_path("t1", "../escape", uuid.uuid4())


def test_build_root_path_is_server_generated(workspace_dir):
    wid = uuid.uuid4()
    path = workspace_service.build_root_path("t1", "u1", wid)
    assert path == (workspace_dir / "t1" / "u1" / str(wid)).resolve()
    assert workspace_dir.resolve() in path.parents


async def test_get_or_create_is_per_user(workspace_dir):
    with tenant_scope("ti-ws"):
        async with async_session_factory() as session:
            async with session.begin():
                a, created_a = await workspace_service.get_or_create(
                    session, tenant_id="ti-ws", user_id="alice"
                )
                b, created_b = await workspace_service.get_or_create(
                    session, tenant_id="ti-ws", user_id="bob"
                )
                a_again, created_again = await workspace_service.get_or_create(
                    session, tenant_id="ti-ws", user_id="alice"
                )

    assert created_a and created_b and not created_again
    assert a.id == a_again.id
    assert a.id != b.id
    assert Path(a.root_path).exists()


async def test_get_owned_rejects_other_user(workspace_dir):
    with tenant_scope("ti-own"):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id="ti-own", user_id="alice"
                )
    with tenant_scope("ti-own"):
        async with async_session_factory() as session:
            with pytest.raises(workspace_service.WorkspaceNotFound):
                await workspace_service.get_owned(
                    session, workspace_id=ws.id, user_id="bob"
                )


# ---- 快照 ------------------------------------------------------------------

def test_snapshot_does_not_recurse(workspace_dir):
    root = workspace_dir / "t1" / "u1" / "w1"
    root.mkdir(parents=True)
    (root / "note.md").write_text("hello", encoding="utf-8")

    # 先打一份，再打第二份——第二份不应把第一份的 snapshots/ 复制进去。
    first = snapshot_service.create_snapshot(root)
    (root / "note.md").write_text("changed", encoding="utf-8")
    second = snapshot_service.create_snapshot(root)

    assert (first.path / "note.md").read_text(encoding="utf-8") == "hello"
    assert (second.path / "note.md").read_text(encoding="utf-8") == "changed"
    assert not (second.path / "snapshots").exists()


async def test_prune_deletes_and_audits(workspace_dir, monkeypatch):
    """同桶里较晚的旧快照应被删；桶最早与最近一份保留，删除写审计。"""
    monkeypatch.setattr(snapshot_service.settings, "snapshot_keep_recent", 1)
    monkeypatch.setattr(snapshot_service.settings, "snapshot_bucket_minutes", 30)
    monkeypatch.setattr(snapshot_service.settings, "snapshot_max_buckets", 48)

    root = workspace_dir / "t1" / "u1" / "w1"
    base = snapshot_service._snapshots_dir(root)

    def make(sid: str, at: datetime) -> snapshot_service.SnapshotInfo:
        path = base / sid
        path.mkdir(parents=True, exist_ok=True)
        (path / "a.txt").write_text("x", encoding="utf-8")
        return snapshot_service.SnapshotInfo(snapshot_id=sid, path=path, created_at=at)

    # 9:00 桶：first(最早) + middle(同桶较晚，应删)；10:00 桶：latest(最近一份)。
    first = make("20261125T090100-a0000001", datetime(2026, 11, 25, 9, 1, tzinfo=UTC))
    middle = make("20261125T092000-b0000002", datetime(2026, 11, 25, 9, 20, tzinfo=UTC))
    latest = make("20261125T100000-c0000003", datetime(2026, 11, 25, 10, 0, tzinfo=UTC))
    now = datetime(2026, 11, 25, 10, 30, tzinfo=UTC)

    with tenant_scope("ti-snap"):
        async with async_session_factory() as session:
            async with session.begin():
                removed = await snapshot_service.prune_snapshots(
                    session,
                    workspace_root=root,
                    tenant_id="ti-snap",
                    workspace_id=uuid.uuid4(),
                    actor_id="u1",
                    now=now,
                )
                audit_rows = (
                    await session.scalars(
                        select(AuditLog).where(
                            AuditLog.event_type == AuditEventType.SNAPSHOT_DELETED
                        )
                    )
                ).all()

    assert middle.snapshot_id in removed
    assert not middle.path.exists()
    assert first.path.exists() and latest.path.exists()
    assert any(row.resource_id == middle.snapshot_id for row in audit_rows)


# ---- 任务状态机 ------------------------------------------------------------

async def test_create_task_is_idempotent():
    with tenant_scope("ti-task"):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id="ti-task", user_id="u1"
                )
                task, created = await create_task(
                    session,
                    tenant_id="ti-task",
                    user_id="u1",
                    workspace_id=ws.id,
                    kind=TaskKind.DOCUMENT,
                    idempotency_key="once-only",
                )
                again, created_again = await create_task(
                    session,
                    tenant_id="ti-task",
                    user_id="u1",
                    workspace_id=ws.id,
                    kind=TaskKind.DOCUMENT,
                    idempotency_key="once-only",
                )

    assert created and not created_again
    assert task.id == again.id


async def test_transition_rejects_illegal_jump():
    with tenant_scope("ti-trans"):
        async with async_session_factory() as session:
            async with session.begin():
                task = Task(
                    tenant_id="ti-trans",
                    user_id="u1",
                    workspace_id=uuid.uuid4(),
                    kind=TaskKind.CODE,
                    idempotency_key="tj",
                )
                session.add(task)
                await session.flush()
                # 合法链路：queued -> running -> succeeded。
                await transition(session, task, TaskStatus.RUNNING)
                await transition(session, task, TaskStatus.SUCCEEDED)
                # 终态无出边。
                with pytest.raises(InvalidTransition):
                    await transition(session, task, TaskStatus.RUNNING)


async def test_cancel_is_request_not_state_write():
    with tenant_scope("ti-cancel"):
        async with async_session_factory() as session:
            async with session.begin():
                task = Task(
                    tenant_id="ti-cancel",
                    user_id="u1",
                    workspace_id=uuid.uuid4(),
                    kind=TaskKind.CODE,
                    idempotency_key="cancel-me",
                    status=TaskStatus.RUNNING,
                )
                session.add(task)
                await session.flush()
                await request_cancel(session, task=task, actor_id="u1")

    assert task.status == TaskStatus.RUNNING  # 状态没被直接改写
    assert task.cancel_requested_at is not None


# ---- 定时触发 --------------------------------------------------------------

async def test_fire_due_schedules_uses_own_tenant_context(workspace_dir):
    """定时触发写入的 task/audit 必须带 schedule 所属租户，不能是 system。"""
    fire_at = datetime.now(UTC) - timedelta(minutes=1)
    with tenant_scope("ti-sched"):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id="ti-sched", user_id="alice"
                )
                row = Schedule(
                    tenant_id="ti-sched",
                    user_id="alice",
                    workspace_id=ws.id,
                    name="每日汇总",
                    kind=TaskKind.SCHEDULE,
                    cron="0 8 * * *",
                    timezone_offset_hours=8,
                    next_fire_at=fire_at,
                )
                session.add(row)
                await session.flush()
                sid = row.id

    fired = await schedule_service.fire_due_schedules(tenant_ids=["ti-sched"])

    assert fired >= 1
    with tenant_scope("ti-sched"):
        async with async_session_factory() as session:
            task = await session.scalar(select(Task).where(Task.user_id == "alice"))
            sched = await session.get(Schedule, sid)
    assert task is not None and task.status == TaskStatus.QUEUED
    assert task.tenant_id == "ti-sched"
    assert sched is not None and sched.last_task_id == task.id
    # SQLite 读回是 naive，统一按 UTC 补 tzinfo 再比较。
    next_at = sched.next_fire_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at > fire_at  # 进度已推进


async def test_fire_due_schedules_is_idempotent(workspace_dir):
    """同一触发点重复扫描只产生一个任务。"""
    fire_at = datetime.now(UTC) - timedelta(minutes=1)
    with tenant_scope("ti-idem"):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id="ti-idem", user_id="u1"
                )
                row = Schedule(
                    tenant_id="ti-idem",
                    user_id="u1",
                    workspace_id=ws.id,
                    name="idem",
                    kind=TaskKind.SCHEDULE,
                    cron="0 8 * * *",
                    next_fire_at=fire_at,
                )
                session.add(row)
                await session.flush()

    await schedule_service.fire_due_schedules(tenant_ids=["ti-idem"])
    await schedule_service.fire_due_schedules(tenant_ids=["ti-idem"])  # 第二次：进度已推进，不应重复建

    with tenant_scope("ti-idem"):
        async with async_session_factory() as session:
            tasks = (await session.scalars(select(Task))).all()
    assert len(tasks) == 1
