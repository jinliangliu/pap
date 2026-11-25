# tests/unit/test_sandbox_pg_store_get.py
"""PgRecordStore.get() 的真实游标语义。

现有 PG 记录测试全部用 FakeStore 顶掉 server 侧的存储，因此只验了
「server 拿到 store 返回后怎么做」，从没执行过 records.py 里的 SQL 读取。
psycopg3 的 AsyncCursor.fetchone/fetchall 也是协程：漏一个 await 时
写库照常成功、回读接口 500，属于只会在真机上暴露的缺陷。

这里用 async 语义的替身直接打 store.get()，把它钉住。
"""

from __future__ import annotations

import pytest

from deploy.sandbox import records

_TENANT = "tenant-a"


class _Cursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Connection:
    def __init__(self) -> None:
        self.settings: list[tuple] = []

    async def execute(self, sql: str, params=None):
        if "set_config" in sql:
            self.settings.append(tuple(params or ()))
            return _Cursor([])
        if "FROM task_executions" in sql:
            return _Cursor(
                [
                    {
                        "execution_id": "exec-1",
                        "user_id": "alice",
                        "status": "succeeded",
                        "cpu_ms": 130,
                        "duration_ms": 140,
                        "exit_code": 0,
                        "policy_revision": None,
                    }
                ]
            )
        if "FROM execution_traces" in sql:
            return _Cursor(
                [
                    {
                        "seq": 0,
                        "from_status": None,
                        "to_status": "queued",
                        "actor": "sandbox",
                        "detail_json": {"user_id": "alice"},
                    },
                    {
                        "seq": 1,
                        "from_status": "queued",
                        "to_status": "succeeded",
                        "actor": "sandbox",
                        "detail_json": {"user_id": "alice"},
                    },
                ]
            )
        if "FROM execution_states" in sql:
            return _Cursor([{"status": "succeeded", "state_json": {}}])
        raise AssertionError(f"未预期的 SQL：{sql}")

    def transaction(self):
        return _Transaction()


class _ConnectionContext:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self) -> None:
        self.conn = _Connection()

    def connection(self):
        return _ConnectionContext(self.conn)


def _store() -> records.RecordStore:
    store = records.RecordStore("postgresql://fake")
    store._pool = _Pool()  # 避免真连库；只替换池，不动被测逻辑
    return store


@pytest.mark.asyncio
async def test_get_awaits_async_cursor_and_returns_plain_dicts():
    store = _store()
    result = await store.get(tenant_id=_TENANT, execution_id="exec-1")

    assert isinstance(result, dict)
    assert result["execution"]["status"] == "succeeded"
    assert result["execution"]["cpu_ms"] == 130
    assert [row["seq"] for row in result["traces"]] == [0, 1]
    assert result["state"]["status"] == "succeeded"
    # RLS 变量必须在同一连接的同一事务里设置，且带上请求租户。
    assert store._pool.conn.settings == [(_TENANT,)]


@pytest.mark.asyncio
async def test_get_returns_none_when_execution_absent():
    store = _store()

    original = store._pool.conn.execute

    async def no_execution(sql, params=None):
        if "FROM task_executions" in sql:
            return _Cursor([])
        return await original(sql, params)

    store._pool.conn.execute = no_execution
    assert await store.get(tenant_id=_TENANT, execution_id="missing") is None
