# tests/unit/test_sandbox_records.py
"""沙箱侧 PG 记录层的 SQL 契约（不连真实 PG）。

真实写入 correctness 只能在部署机验证；这里固定的是「写错就会破坏安全语义」的部分：
- set_config 必须是事务级（true），否则会污染连接池里的下一条请求；
- 终态、trace、state 必须在同一个事务块里；
- 三个 upsert 都要 ON CONFLICT，否则网络重试会写出两行；
- 写入方必须用执行记录的 tenant_id 过 RLS，不能用 system 绕过。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deploy.sandbox import records


class FakeTransaction:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def __aenter__(self):
        self.calls.append(("transaction", "enter"))
        return self

    async def __aexit__(self, *_args):
        self.calls.append(("transaction", "exit"))
        return None


class FakeConnection:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append(("execute", sql, params))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self.calls)


class FakePool:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def connection(self):
        calls = self.calls

        class _Ctx:
            async def __aenter__(self):
                calls.append(("connection", "enter"))
                return FakeConnection(calls)

            async def __aexit__(self, *_args):
                calls.append(("connection", "exit"))
                return None

        return _Ctx()


def _store() -> tuple[records.RecordStore, list]:
    store = records.RecordStore("postgresql://unused")
    calls: list = []
    store._pool = FakePool(calls)  # type: ignore[assignment]
    return store, calls


@pytest.fixture
def record() -> dict:
    return {
        "execution_id": "exec-1",
        "status": "succeeded",
        "user_id": "u1",
        "cpu_ms": 12,
        "duration_ms": 40,
        "exit_code": 0,
        "truncated": False,
    }


async def test_set_tenant_uses_transaction_scope(record):
    store, calls = _store()
    await store.save(tenant_id="tenant-a", record=record)

    set_calls = [c for c in calls if c[0] == "execute" and "set_config" in c[1]]
    assert len(set_calls) == 1
    assert set_calls[0][2] == ("tenant-a",)
    assert "true" in set_calls[0][1]  # 事务级，不是会话级


async def test_write_is_one_transaction(record):
    store, calls = _store()
    await store.save(tenant_id="tenant-a", record=record)

    tx_enters = [c for c in calls if c == ("transaction", "enter")]
    tx_exits = [c for c in calls if c == ("transaction", "exit")]
    assert len(tx_enters) == 1
    assert len(tx_exits) == 1

    # 三条写入都在 set_config 之后、事务结束之前。
    sqls = [c[1] for c in calls if c[0] == "execute"]
    assert len(sqls) == 5  # set_config + execution + 2 traces + state
    assert "INSERT INTO task_executions" in sqls[1]
    assert "INSERT INTO execution_traces" in sqls[2]
    assert "INSERT INTO execution_traces" in sqls[3]
    assert "INSERT INTO execution_states" in sqls[4]


async def test_upserts_are_idempotent(record):
    store, calls = _store()
    await store.save(tenant_id="tenant-a", record=record)

    sqls = [c[1] for c in calls if c[0] == "execute"]
    # execution + 两条 trace + state，四 upsert 都要幂等。
    assert sum("ON CONFLICT" in sql for sql in sqls) == 4


async def test_tenant_id_comes_from_argument_not_record(record):
    store, calls = _store()
    # record 里即使混进一个 tenant_id，也必须用方法参数的那个。
    await store.save(tenant_id="tenant-real", record={**record, "tenant_id": "tenant-fake"})

    execution_call = next(
        c for c in calls if c[0] == "execute" and "INSERT INTO task_executions" in c[1]
    )
    assert execution_call[2]["tenant_id"] == "tenant-real"


async def test_owner_is_written_into_execution_row(record):
    store, calls = _store()
    await store.save(tenant_id="tenant-a", record=record)

    execution_call = next(
        c for c in calls if c[0] == "execute" and "INSERT INTO task_executions" in c[1]
    )
    assert execution_call[2]["user_id"] == "u1"
    assert "user_id" in records._UPSERT_EXECUTION
    assert "user_id" in records._READ_EXECUTION


async def test_trace_detail_carries_owner(record):
    store, calls = _store()
    await store.save(tenant_id="tenant-a", record=record)

    trace_calls = [
        c for c in calls if c[0] == "execute" and "INSERT INTO execution_traces" in c[1]
    ]
    assert len(trace_calls) == 2
    assert [c[2]["seq"] for c in trace_calls] == [0, 1]
    for call in trace_calls:
        assert json.loads(call[2]["detail_json"])["user_id"] == "u1"


async def test_missing_optional_fields_become_none():
    store, calls = _store()
    await store.save(
        tenant_id="tenant-a",
        record={"execution_id": "exec-min", "status": "timeout"},
    )

    execution_call = next(
        c for c in calls if c[0] == "execute" and "INSERT INTO task_executions" in c[1]
    )
    for key in ("cpu_ms", "duration_ms", "exit_code", "task_id"):
        assert execution_call[2][key] is None
