# tests/unit/test_maintenance_leader.py
"""维护任务必须做到「多实例同一时刻只有一个在扫」。

没有互斥时每个进程都会过期审批、回收卡死记录，日志与数据库压力都乘以实例数。
实现用 PostgreSQL 事务级 advisory lock：抢到才扫，事务结束自动释放。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.workers import maintenance


class FakeSession:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.committed = False
        self.rolled_back = False
        self.closed = False

    async def scalar(self, statement, params=None):  # noqa: ANN001, ANN201
        return self.acquired

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


def _patched(
    monkeypatch,
    *,
    is_postgres: bool,
    acquired: bool | None,
    require_leader: bool = True,
):  # noqa: ANN001, ANN202
    calls: list[int] = []
    sessions: list[FakeSession] = []

    async def fake_sweeps() -> None:
        calls.append(1)

    def factory() -> FakeSession:
        session = FakeSession(bool(acquired))
        sessions.append(session)
        return session

    monkeypatch.setattr(maintenance, "_run_sweeps", fake_sweeps)
    monkeypatch.setattr(
        maintenance,
        "settings",
        SimpleNamespace(
            maintenance_require_leader=require_leader,
            is_postgres=is_postgres,
            maintenance_advisory_lock_key=1347375181,
        ),
    )
    monkeypatch.setattr(maintenance, "async_session_factory", factory)

    return calls, sessions


async def test_skips_when_another_instance_holds_the_lock(monkeypatch):
    calls, sessions = _patched(monkeypatch, is_postgres=True, acquired=False)

    await maintenance.run_maintenance_once()

    assert calls == []
    assert sessions and sessions[0].rolled_back is True


async def test_runs_and_releases_lock_when_acquired(monkeypatch):
    calls, sessions = _patched(monkeypatch, is_postgres=True, acquired=True)

    await maintenance.run_maintenance_once()

    assert calls == [1]
    assert sessions[0].committed is True
    assert sessions[0].closed is True


async def test_sqlite_runs_without_advisory_lock(monkeypatch):
    """本地/测试用 SQLite，没有 advisory lock，单进程直接跑。"""

    def factory():  # noqa: ANN202
        raise AssertionError("SQLite 下不应去抢 advisory lock")

    calls: list[int] = []

    async def fake_sweeps() -> None:
        calls.append(1)

    monkeypatch.setattr(maintenance, "_run_sweeps", fake_sweeps)
    monkeypatch.setattr(
        maintenance,
        "settings",
        SimpleNamespace(
            maintenance_require_leader=True,
            is_postgres=False,
            maintenance_advisory_lock_key=1347375181,
        ),
    )
    monkeypatch.setattr(maintenance, "async_session_factory", factory)

    await maintenance.run_maintenance_once()

    assert calls == [1]


async def test_leader_lock_can_be_disabled(monkeypatch):
    calls, sessions = _patched(
        monkeypatch,
        is_postgres=True,
        acquired=None,
        require_leader=False,
    )

    await maintenance.run_maintenance_once()

    assert calls == [1]
    assert sessions == []
