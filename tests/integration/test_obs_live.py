# tests/integration/test_obs_live.py
"""运行视图 L0：起始行、残行清扫、`/live`。

四条性质，每条都能被一处实现改动推翻——所以每条都要能反向验证：

1. **起始行在收尾之前就在库里。** 去掉 `_submit_begin` 就红。
2. **收尾是 UPDATE，不是再加一行。** 改成 `session.add` 会撞 `trace_id` 唯一约束，
   整批写入失败——测试会看到一条永远 `running` 的行。
3. **采样率不到 1 时一条起始行都不写。** 否则被采掉的那轮留下永不收尾的假活人。
4. **残行只标记，不删除。** 「跑到一半进程没了」本身是要被看见的事实。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.approval import ApprovalRequest, ApprovalStatus, RiskLevel
from app.models.observability import ObsSpan, ObsTrace
from app.observability.context import trace_run
from app.observability.writer import ObsWriter
from app.services.obs_sweep import ORPHAN_CATEGORY, sweep_orphaned_runs
from app.tenancy.context import tenant_scope

TENANT = "default"


async def _load(trace_id: str) -> ObsTrace | None:
    """用新会话读：当前 session 的 identity map 会缓存住旧状态。"""
    async with async_session_factory() as session:
        return await session.scalar(select(ObsTrace).where(ObsTrace.trace_id == trace_id))


async def _wait_trace(trace_id: str, *, tries: int = 60, delay: float = 0.1) -> ObsTrace | None:
    """等起始行落库。写入器 1 秒攒一批，所以要轮询而不是立刻断言。"""
    for _ in range(tries):
        row = await _load(trace_id)
        if row is not None:
            return row
        await asyncio.sleep(delay)
    return None


async def _clean(session) -> None:  # noqa: ANN001
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.execute(
        delete(ApprovalRequest).where(ApprovalRequest.idempotency_key.like("obs-live-%"))
    )
    await session.commit()


def _running(*, user_id: str, started_at: datetime, agent_name: str | None = None) -> ObsTrace:
    return ObsTrace(
        tenant_id=TENANT,
        trace_id=uuid.uuid4().hex,
        user_id=user_id,
        started_at=started_at,
        status="running",
        agent_name=agent_name,
    )


def _approval(
    *,
    user_id: str,
    key: str,
    created_at: datetime,
    status: ApprovalStatus = ApprovalStatus.PENDING,
) -> ApprovalRequest:
    return ApprovalRequest(
        tenant_id=TENANT,
        user_id=user_id,
        thread_id=f"thread-{key}",
        run_id=f"run-{key}",
        agent_name="ops",
        delegation_chain=[{"id": "agent-cs", "name": "cs"}],
        tool_name="create_maintenance_ticket",
        tool_display_name="创建维护工单",
        risk_level=RiskLevel.WRITE,
        status=status,
        reason="演练",
        parameters_json={},
        parameter_hash="hash",
        idempotency_key=f"obs-live-{key}",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
    )


async def test_start_row_lands_before_the_turn_finishes(session, monkeypatch):
    """跑着的这一轮必须已经能被查到，且收尾后仍然只有一行。"""
    monkeypatch.setattr(settings, "obs_enabled", True)
    writer = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", writer)
    await _clean(session)

    await writer.start()
    try:
        async with trace_run(
            tenant_id=TENANT,
            user_id="obs-live-user",
            thread_id="thread-obs-live",
            agent_name="ops",
            input_text="在吗",
        ) as trace:
            assert trace is not None
            row = await _wait_trace(trace.trace_id)
            assert row is not None, "收尾之前库里就该有这一行（RUNTIME_VIEW.md §5.1）"
            assert row.status == "running"
            assert row.ended_at is None
            assert row.thread_id == "thread-obs-live"
            assert row.agent_name == "ops"
            trace.mark(status="ok", intent="chat", output_text="在。")
    finally:
        await writer.stop()

    rows = (await session.scalars(select(ObsTrace).where(ObsTrace.trace_id == trace.trace_id))).all()
    assert len(rows) == 1, "收尾必须是 UPDATE：多出一行说明起始行与收尾行各写了一次"
    assert rows[0].status == "ok"
    assert rows[0].ended_at is not None
    assert rows[0].output_text == "在。"


async def test_sampled_out_trace_leaves_no_running_row(session, monkeypatch):
    """采样率 < 1 时起始行一条都不写：否则会留下永远不会收尾的假活人。"""
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_sample_rate", 0.0)
    writer = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", writer)
    await _clean(session)

    await writer.start()
    try:
        async with trace_run(tenant_id=TENANT, user_id="obs-live-user") as trace:
            assert trace is not None
    finally:
        await writer.stop()

    row = await _load(trace.trace_id)
    assert row is None, "被采掉的一轮不该在库里留下任何行"


async def test_orphaned_running_is_marked_not_deleted(session, monkeypatch):
    """超过 TTL 的 running 变 abandoned；还在跑的不动；行必须留着。"""
    monkeypatch.setattr(settings, "obs_enabled", True)
    await _clean(session)

    now = datetime.now(UTC)
    orphan = _running(user_id="obs-live-user", started_at=now - timedelta(hours=2))
    alive = _running(user_id="obs-live-user", started_at=now - timedelta(seconds=60))
    session.add_all([orphan, alive])
    await session.commit()

    with tenant_scope(TENANT):
        swept = await sweep_orphaned_runs(ttl_seconds=1800)
    assert swept == 1, "只该扫到那条两小时前开始的"

    gone = await _load(orphan.trace_id)
    assert gone is not None, "清扫是标记，不是删除——删掉等于把事故抹平"
    assert gone.status == "abandoned"
    assert gone.error_category == ORPHAN_CATEGORY
    # 不知道它什么时候停的，就别编一个时间：编了「耗时」会凭空多出一段。
    assert gone.ended_at is None
    assert gone.duration_ms is None

    still = await _load(alive.trace_id)
    assert still is not None
    assert still.status == "running", "一分钟前开始的那轮还在跑，不能当成残行"


async def test_live_lists_running_and_waiting_with_stale_flags(
    client, session, seeded_identity, monkeypatch
):
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_live_stale_seconds", 900)
    await _clean(session)

    user_id = seeded_identity["user_id"]
    now = datetime.now(UTC)

    fresh = _running(user_id=user_id, started_at=now - timedelta(seconds=30), agent_name="ops")
    stale = _running(user_id=user_id, started_at=now - timedelta(minutes=40))
    finished = ObsTrace(
        tenant_id=TENANT,
        trace_id=uuid.uuid4().hex,
        user_id=user_id,
        started_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
        duration_ms=1000,
        status="ok",
    )
    waiting = _approval(user_id=user_id, key="wait", created_at=now - timedelta(minutes=20))
    expired = _approval(
        user_id=user_id,
        key="expired",
        created_at=now - timedelta(minutes=40),
        status=ApprovalStatus.EXPIRED,
    )
    session.add_all([fresh, stale, finished, waiting, expired])
    await session.commit()

    response = await client.get("/api/v1/observability/live")
    assert response.status_code == 200
    body = response.json()

    assert body["enabled"] is True
    # 已收尾的那条不该出现：这张视图回答的是「现在」，不是「今天」。
    assert {r["trace_id"] for r in body["running"]} == {fresh.trace_id, stale.trace_id}
    assert {r["trace_id"] for r in body["running"] if r["stale"]} == {stale.trace_id}
    assert body["stale_after_ms"] == 900_000

    # 库里可能还有别的用例留下的 PENDING 单（同一个 sqlite 文件），
    # 所以断「我这张在、过期那张不在」，而不是断「总共只有一条」。
    ids = {w["approval_id"] for w in body["waiting"]}
    assert str(waiting.id) in ids
    assert str(expired.id) not in ids, "只有 PENDING 的单子算「在等」"

    item = next(w for w in body["waiting"] if w["approval_id"] == str(waiting.id))
    assert item["delegated_from"] == "cs", "委派链最后一项是直接上级"
    assert item["waited_ms"] >= 19 * 60 * 1000
    assert item["stale"] is True


async def test_live_self_scope_hides_other_peoples_runs(
    client, session, seeded_identity, monkeypatch
):
    """没有 observability:read 的成员只看到自己发起的那部分。"""
    monkeypatch.setattr(settings, "obs_enabled", True)
    await _clean(session)

    now = datetime.now(UTC)
    mine = _running(user_id=seeded_identity["user_id"], started_at=now - timedelta(seconds=10))
    others = _running(user_id="obs-live-somebody-else", started_at=now - timedelta(seconds=20))
    session.add_all([mine, others])
    await session.commit()

    response = await client.get("/api/v1/observability/live")
    body = response.json()

    assert body["scope"] == "self"
    assert {r["trace_id"] for r in body["running"]} == {mine.trace_id}


async def test_running_trace_is_not_counted_as_an_error(
    client, session, seeded_identity, monkeypatch
):
    """正在跑的一轮既不是成功也不是失败。

    把它算成错误，每个刚点发送的轮次都会让错误率跳一下——这是最容易让人
    不再相信这个数字的方式。而 `abandoned`（跑到一半进程没了）仍然是事故，
    不能被顺手一起放过。
    """
    monkeypatch.setattr(settings, "obs_enabled", True)
    await _clean(session)

    now = datetime.now(UTC)
    user_id = seeded_identity["user_id"]
    same_start = now - timedelta(minutes=1)

    session.add_all(
        [
            ObsTrace(
                tenant_id=TENANT,
                trace_id=uuid.uuid4().hex,
                user_id=user_id,
                started_at=same_start,
                ended_at=same_start,
                duration_ms=1000,
                status="ok",
            ),
            _running(user_id=user_id, started_at=same_start),
            ObsTrace(
                tenant_id=TENANT,
                trace_id=uuid.uuid4().hex,
                user_id=user_id,
                started_at=same_start,
                status="abandoned",
                error_category=ORPHAN_CATEGORY,
            ),
        ]
    )
    await session.commit()

    body = (await client.get("/api/v1/observability/overview", params={"hours": 1})).json()

    assert body["traces"] == 3
    assert body["errors"] == 1, "只有 abandoned 算错误，running 不算"
    assert round(body["error_rate"], 2) == 0.33


async def test_live_reports_disabled_instead_of_404(client, monkeypatch):
    """关掉观测时告诉界面「看不到了」，而不是「这个接口不存在」。"""
    monkeypatch.setattr(settings, "obs_enabled", False)

    response = await client.get("/api/v1/observability/live")
    assert response.status_code == 200

    body = response.json()
    assert body["enabled"] is False
    assert body["running"] == []
    assert body["waiting"] == []
