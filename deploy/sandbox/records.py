"""沙箱侧执行记录的持久化（直写 PG）。

独立模块：沙箱服务在 /opt/pap-sandbox 下用**自己的虚拟环境**运行，只装 fastapi /
uvicorn / psycopg，**import 不到主仓的 app.models**。所以这里用裸 SQL，表结构以
主仓 `alembic/versions/b9d4f2a71c60_transaction_tables.py` 为准。

三条硬约束（见 docs/TRANSACTION.md §3、§7）：

1. **终态与 Trace/State 同一事务提交。** 平台 verifier 回读时要么看到完整的一份，
   要么什么都看不到；不能出现「有终态、没 trace」这种半截事实。
2. **set_config('app.tenant_id', ..., true) 用事务级。** 不用超级用户绕过 RLS：
   沙箱对自己写入的行也要受同一道隔离约束，否则「可信服务」就成了越权通道。
3. **写入幂等。** execution_id 冲突时 DO NOTHING：网络重试、审批连点不能写两行。

配置缺失（PAP_SANDBOX_DATABASE_URL 为空）时 `get_store()` 返回 None，
由 server 回落到原有的内存记录——这是 `SANDBOX_ROADMAP.md` §4 要求的回滚路径，
不是「静默降级」：server 会在启动时明确告警。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("pap.sandbox.records")

_SET_TENANT_SQL = "SELECT set_config('app.tenant_id', %s, true)"

_UPSERT_EXECUTION = """
INSERT INTO task_executions
    (id, tenant_id, execution_id, task_id, user_id, status,
     cpu_ms, duration_ms, exit_code, policy_revision,
     snapshot_id, stdout_object_key, stderr_object_key, truncated,
     error_code, created_at, finished_at)
VALUES
    (gen_random_uuid(), %(tenant_id)s, %(execution_id)s, %(task_id)s, %(user_id)s,
     %(status)s, %(cpu_ms)s, %(duration_ms)s, %(exit_code)s, %(policy_revision)s,
     %(snapshot_id)s, %(stdout_object_key)s, %(stderr_object_key)s, %(truncated)s,
     %(error_code)s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (tenant_id, execution_id) DO NOTHING
"""

_INSERT_TRACE = """
INSERT INTO execution_traces
    (id, tenant_id, execution_id, seq, from_status, to_status, actor, detail_json, occurred_at)
VALUES
    (gen_random_uuid(), %(tenant_id)s, %(execution_id)s, %(seq)s, %(from_status)s,
     %(to_status)s, %(actor)s, %(detail_json)s::jsonb, CURRENT_TIMESTAMP)
ON CONFLICT (tenant_id, execution_id, seq) DO NOTHING
"""

_UPSERT_STATE = """
INSERT INTO execution_states
    (id, tenant_id, execution_id, status, state_json, updated_at)
VALUES
    (gen_random_uuid(), %(tenant_id)s, %(execution_id)s, %(status)s, %(state_json)s::jsonb, CURRENT_TIMESTAMP)
ON CONFLICT (tenant_id, execution_id) DO UPDATE
SET status = EXCLUDED.status,
    state_json = EXCLUDED.state_json,
    updated_at = CURRENT_TIMESTAMP
"""

_READ_EXECUTION = """
SELECT execution_id, user_id, status, cpu_ms, duration_ms, exit_code, policy_revision
FROM task_executions
WHERE tenant_id = %(tenant_id)s AND execution_id = %(execution_id)s
"""

_READ_TRACE = """
SELECT seq, from_status, to_status, actor, detail_json
FROM execution_traces
WHERE tenant_id = %(tenant_id)s AND execution_id = %(execution_id)s
ORDER BY seq
"""

_READ_STATE = """
SELECT status, state_json
FROM execution_states
WHERE tenant_id = %(tenant_id)s AND execution_id = %(execution_id)s
"""


class RecordStore:
    """异步 PG 连接池。一个沙箱进程一个实例， lifespan 里开、关。"""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                # 不用 autocommit：写入必须在一个事务里，set_config 才是事务级的。
                "row_factory": dict_row,
                "prepare_threshold": 0,
            },
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=15)

    async def close(self) -> None:
        await self._pool.close()

    async def save(self, *, tenant_id: str, record: dict[str, Any]) -> None:
        """终态 + 轨迹 + 当前态一次提交。

        `record` 的键由 server 组装；缺失的可选字段统一为 None，不留「键不存在」
        与「值为 NULL」两种写法——SQL 参数必须齐全，ON CONFLICT 才能稳定命中。
        """
        execution_id = str(record["execution_id"])
        status = str(record["status"])

        # 归属写进轨迹而非只用 execution 表：回读时不额外开一条查询，
        # 同租户内按人隔离也能成立（猜到别人的 ID 拿不到内容）。
        detail = json.dumps({"user_id": record.get("user_id") or ""}, ensure_ascii=False)

        async with self._pool.connection() as conn:
            await conn.execute(_SET_TENANT_SQL, (tenant_id,))

            async with conn.transaction():
                row = {
                    "tenant_id": tenant_id,
                    "execution_id": execution_id,
                    "task_id": record.get("task_id"),
                    "user_id": record.get("user_id") or "",
                    "status": status,
                    "cpu_ms": record.get("cpu_ms"),
                    "duration_ms": record.get("duration_ms"),
                    "exit_code": record.get("exit_code"),
                    "policy_revision": record.get("policy_revision"),
                    "snapshot_id": record.get("snapshot_id"),
                    "stdout_object_key": record.get("stdout_object_key"),
                    "stderr_object_key": record.get("stderr_object_key"),
                    "truncated": bool(record.get("truncated", False)),
                    "error_code": record.get("error_code"),
                }
                await conn.execute(_UPSERT_EXECUTION, row)

                # 轨迹从 queued 开始：执行已发生这件事本身也是事实，
                # 否则「只看到 succeeded」无法证明中间没有跳过审批。
                await conn.execute(
                    _INSERT_TRACE,
                    {
                        "tenant_id": tenant_id,
                        "execution_id": execution_id,
                        "seq": 0,
                        "from_status": None,
                        "to_status": "queued",
                        "actor": "sandbox",
                        "detail_json": detail,
                    },
                )
                await conn.execute(
                    _INSERT_TRACE,
                    {
                        "tenant_id": tenant_id,
                        "execution_id": execution_id,
                        "seq": 1,
                        "from_status": "queued",
                        "to_status": status,
                        "actor": "sandbox",
                        "detail_json": detail,
                    },
                )
                await conn.execute(
                    _UPSERT_STATE,
                    {
                        "tenant_id": tenant_id,
                        "execution_id": execution_id,
                        "status": status,
                        "state_json": "{}",
                    },
                )

    async def get(self, *, tenant_id: str, execution_id: str) -> dict[str, Any] | None:
        """回读终态、轨迹与当前态，三份都在同一租户作用域下取。

        查不到返回 None，由 server 翻成 404——「查不到」和「不存在」对外一致，
        不泄露别的租户有没有这个 ID。
        """
        async with self._pool.connection() as conn:
            await conn.execute(_SET_TENANT_SQL, (tenant_id,))

            async with conn.transaction():
                # psycopg3 的 AsyncConnection.execute() 返回 AsyncCursor，
                # fetchone/fetchall 本身也是协程。漏 await 时这里不会报语法错，
                # 而是把 coroutine 当 Row 往下传，回读接口 500——写库正常、
                # 回读全崩，最容易在真上线时才暴露。
                cur = await conn.execute(
                    _READ_EXECUTION,
                    {"tenant_id": tenant_id, "execution_id": execution_id},
                )
                execution = await cur.fetchone()

                if execution is None:
                    return None

                cur = await conn.execute(
                    _READ_TRACE,
                    {"tenant_id": tenant_id, "execution_id": execution_id},
                )
                traces = await cur.fetchall()
                cur = await conn.execute(
                    _READ_STATE,
                    {"tenant_id": tenant_id, "execution_id": execution_id},
                )
                state = await cur.fetchone()

        # [::] 转普通 dict：dict_row 返回的是可变的 Row，别把池内对象交出去。
        return {
            "execution": dict(execution),
            "traces": [dict(row) for row in traces],
            "state": dict(state) if state is not None else None,
        }
