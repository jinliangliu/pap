# tests/integration/test_obs_operation.py
"""运行视图 L3：一次运行（跨轮、跨 Agent）。

这个接口存在的理由只有一个：**把「等了多久」和「跑了多久」分开**。
所以三条用例都冲着「有人把两个数合并了」这件事写：

1. 一段发起 + 一张单子 + 一段执行：`wall_ms ≈ waiting_ms + active_ms`，
   且 `wall_ms > active_ms`——把等待算进系统耗时，第 1 条立刻红。
2. 两张单子时间重叠：等待取**区间并集**，不是求和。求和会算出比总历时
   还长的「等人工」，而这个错只有并行审批时才显形。
3. 别人的运行 `self` 身份拿到 404 而不是 403：403 等于告诉对方 id 存在。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.main import app
from app.models.approval import ApprovalRequest, ApprovalStatus, RiskLevel
from app.models.observability import ObsSpan, ObsTrace
from app.models.rbac import Role, RolePermission, UserRole

TENANT = "default"
PLAIN_SUBJECT = "op-plain"
READER_SUBJECT = "op-reader"


async def _client_for(subject: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Authenticated-Subject": subject, "X-Authenticated-Tenant": TENANT},
    )


async def _role(session, name: str, permissions: tuple[str, ...]) -> Role:  # noqa: ANN001
    role = await session.scalar(select(Role).where(Role.name == name))
    if role is None:
        role = Role(name=name, description=name)
        session.add(role)
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))
        await session.flush()
    return role


async def _member(session, *, subject: str, role: Role) -> None:  # noqa: ANN001
    from app.models.rbac import User

    user = await session.scalar(select(User).where(User.external_subject == subject))
    if user is None:
        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()

    exists = await session.scalar(
        select(UserRole).where(UserRole.tenant_id == TENANT, UserRole.user_id == user.id)
    )
    if exists is None:
        session.add(UserRole(tenant_id=TENANT, user_id=user.id, role_id=role.id))
        await session.flush()


async def _clean(session) -> None:  # noqa: ANN001
    # 先按 thread 找出本文件写的 trace，再按 trace_id 删 span：
    # `obs_spans` 上没有 thread 列，直接 `delete(ObsSpan)` 会把别的用例
    # （真实对话链路留下的）明细一起清掉。
    trace_ids = (
        await session.scalars(
            select(ObsTrace.trace_id).where(ObsTrace.thread_id.like("thread-op%"))
        )
    ).all()
    if trace_ids:
        await session.execute(delete(ObsSpan).where(ObsSpan.trace_id.in_(trace_ids)))
    await session.execute(delete(ObsTrace).where(ObsTrace.thread_id.like("thread-op%")))
    await session.execute(
        delete(ApprovalRequest).where(ApprovalRequest.idempotency_key.like("obs-op-%"))
    )
    await session.commit()


@pytest.fixture
async def op_env(session, seeded_identity):  # noqa: ANN001
    await _clean(session)
    plain_role = await _role(session, "op_plain", ("tools:read",))
    reader_role = await _role(session, "op_reader", ("tools:read", "observability:read"))
    await _member(session, subject=PLAIN_SUBJECT, role=plain_role)
    await _member(session, subject=READER_SUBJECT, role=reader_role)
    await session.commit()
    yield
    await _clean(session)


def _trace(
    *,
    user_id: str,
    operation_id: str,
    started_at: datetime,
    ended_at: datetime | None,
    status: str = "ok",
    agent_name: str | None = None,
) -> ObsTrace:
    return ObsTrace(
        tenant_id=TENANT,
        trace_id=uuid.uuid4().hex,
        operation_id=operation_id,
        user_id=user_id,
        thread_id="thread-op-main",
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=int((ended_at - started_at).total_seconds() * 1000) if ended_at else None,
        status=status,
        agent_name=agent_name,
    )


def _approval(
    *,
    user_id: str,
    operation_id: str,
    key: str,
    created_at: datetime,
    approved_at: datetime | None = None,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    agent_name: str = "op-ops",
    delegated_from: str | None = "op-cs",
) -> ApprovalRequest:
    return ApprovalRequest(
        tenant_id=TENANT,
        user_id=user_id,
        thread_id=f"thread-op-{key}",
        run_id=f"run-{key}",
        agent_name=agent_name,
        delegation_chain=(
            [{"id": "agent-op-cs", "name": delegated_from}] if delegated_from else None
        ),
        operation_id=operation_id,
        tool_name="create_maintenance_ticket",
        tool_display_name="创建维护工单",
        risk_level=RiskLevel.WRITE,
        status=status,
        reason="把 3 号站的压力调到 0.4MPa",
        parameters_json={"station": "3"},
        parameter_hash="hash",
        idempotency_key=f"obs-op-{key}",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
        approved_at=approved_at,
    )


def _agent_span(*, trace_id: str, agent_name: str, parent: str | None, at: datetime, ms: int):
    return ObsSpan(
        tenant_id=TENANT,
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=None,
        seq=0,
        name=f"agent.{agent_name}",
        kind="agent",
        agent_name=agent_name,
        started_at=at,
        ended_at=at + timedelta(milliseconds=ms),
        duration_ms=ms,
        status="ok",
        attrs={"agent": agent_name, "depth": 1, "delegated_from": parent, "tool_calls": 2},
    )


async def test_operation_separates_wall_waiting_and_active(op_env, session, seeded_identity):
    """等 2 分钟的写操作：历时 2 分 4 秒，其中 2 分钟在等人。

    把 `waiting_ms` 并进 `wall_ms`（或者干脆只给一个「总耗时」），
    这条用例就分不清「系统慢」和「人慢」——而那正是这个接口的全部意义。
    """
    user_id = seeded_identity["user_id"]
    operation_id = uuid.uuid4().hex
    base = datetime.now(UTC) - timedelta(minutes=30)

    session.add_all(
        [
            # 第 1 轮：发起，2 秒后停在审批上。
            _trace(
                user_id=user_id,
                operation_id=operation_id,
                started_at=base,
                ended_at=base + timedelta(seconds=2),
                status="awaiting_approval",
                agent_name="op-cs",
            ),
            # 第 2 轮：确认之后执行，3 秒跑完。
            _trace(
                user_id=user_id,
                operation_id=operation_id,
                started_at=base + timedelta(seconds=121),
                ended_at=base + timedelta(seconds=124),
                status="ok",
                agent_name="op-ops",
            ),
        ]
    )
    session.add(
        _approval(
            user_id=user_id,
            operation_id=operation_id,
            key="single",
            created_at=base + timedelta(seconds=1),
            approved_at=base + timedelta(seconds=121),
            status=ApprovalStatus.VERIFIED,
        )
    )
    await session.commit()

    async with await _client_for(seeded_identity["subject"]) as http:
        response = await http.get(f"/api/v1/observability/operations/{operation_id}")

    assert response.status_code == 200
    body = response.json()

    assert len(body["traces"]) == 2

    # 历时 = 第 1 轮开始 → 第 2 轮结束 = 124 秒。
    assert abs(body["wall_ms"] - 124_000) <= 2_000
    # 等人工 = 单子从 1 秒挂到 121 秒 = 120 秒。
    assert abs(body["waiting_ms"] - 120_000) <= 2_000
    # 系统实际在跑 = 124 - 120 = 4 秒（2 秒发起 + 3 秒执行有一次重叠，
    # 这里只断言量级：它必须是个位数千毫秒，绝不能是 124 秒）。
    assert body["active_ms"] <= 6_000
    assert body["wall_ms"] == body["waiting_ms"] + body["active_ms"]

    # 批准之后这一轮落在 op-ops 名下，发起那一轮在 op-cs 名下。
    assert [item["agent_name"] for item in body["traces"]] == ["op-cs", "op-ops"]

    approval = body["approvals"][0]
    assert approval["agent_name"] == "op-ops"
    # 委派链给名字：这一栏是给复核的人读的。
    assert approval["delegation_chain"] == ["op-cs"]
    assert approval["delegated_from"] == "op-cs"
    assert approval["status"] == "VERIFIED"
    # 审批卡上那一句要能重新拼出来（A2A.md §9）。
    assert "0.4MPa" in approval["reason"]

    # 第 1 轮是「停在审批上」收尾的，但它后面还有第 2 轮——那个暂停早处理完了。
    # 不抹掉它，这次运行会永远显示「等人工」。
    assert body["display_state"] == "completed"
    assert body["attention"] is False

    participants = {item["agent_name"]: item for item in body["participants"]}
    assert participants["op-cs"]["role"] == "initiator"
    assert participants["op-ops"]["role"] == "initiator"  # 自己也跑了一轮
    assert participants["op-ops"]["traces"] == 1
    # 等待时间记在「要执行的 Agent」账上，不是发起方。
    assert participants["op-ops"]["waiting_ms"] >= 100_000
    assert participants["op-cs"]["waiting_ms"] == 0

    # 时间线：发起 → 单子 → 执行。
    assert [step["kind"] for step in body["steps"]] == ["trace", "approval", "trace"]


async def test_operation_waiting_is_union_not_sum(op_env, session, seeded_identity):
    """两张单子同时挂着：等待取并集。

    求和会把同一段时间算两遍，算出来的「等人工」比总历时还长——只有并行
    审批时才显形，而并行审批恰恰是最需要看清「这次到底卡了多久」的时候。
    """
    user_id = seeded_identity["user_id"]
    operation_id = uuid.uuid4().hex
    base = datetime.now(UTC) - timedelta(hours=1)

    session.add(
        _trace(
            user_id=user_id,
            operation_id=operation_id,
            started_at=base,
            ended_at=base + timedelta(seconds=5),
            status="awaiting_approval",
            agent_name="op-cs",
        )
    )
    session.add_all(
        [
            # [0s, 120s)
            _approval(
                user_id=user_id,
                operation_id=operation_id,
                key="wide",
                created_at=base,
                approved_at=base + timedelta(seconds=120),
                agent_name="op-a",
            ),
            # [30s, 90s)，整段落在上一条里。求和 = 180 秒，并集 = 120 秒。
            _approval(
                user_id=user_id,
                operation_id=operation_id,
                key="nested",
                created_at=base + timedelta(seconds=30),
                approved_at=base + timedelta(seconds=90),
                agent_name="op-b",
            ),
        ]
    )
    await session.commit()

    async with await _client_for(seeded_identity["subject"]) as http:
        body = (await http.get(f"/api/v1/observability/operations/{operation_id}")).json()

    assert len(body["approvals"]) == 2
    assert abs(body["waiting_ms"] - 120_000) <= 2_000
    # 求和会得到 180 秒，这里必须明显小于它。
    assert body["waiting_ms"] < 150_000
    # 等待不能超过历时：超过了就说明算了两遍。
    assert body["waiting_ms"] <= body["wall_ms"]
    assert body["active_ms"] == body["wall_ms"] - body["waiting_ms"]


async def test_operation_state_follows_the_unfinished_part(op_env, session, seeded_identity):
    """聚合状态看的是「还有什么没做完」，不是「出现过的最糟状态」。

    两个子场景，因为它们的结论不同、界面上的动作也不同：

    - trace 已收尾、单子还挂着 → 等人工（有人点一下就好了），
      这时不该再叠一个「需要注意」——它本来就在等人看；
    - trace 已 error、单子还挂着 → 失败 **且** 需要注意。
      这不是等待：没有人会去恢复它（`state.py` 排第 2 的那条规则）。
      显示成「等人工」会让人以为点一下确认就好了。
    """
    user_id = seeded_identity["user_id"]
    base = datetime.now(UTC) - timedelta(minutes=10)

    paused_op = uuid.uuid4().hex
    session.add(
        _trace(
            user_id=user_id,
            operation_id=paused_op,
            started_at=base,
            ended_at=base + timedelta(seconds=3),
            status="ok",
            agent_name="op-cs",
        )
    )
    session.add(
        _approval(
            user_id=user_id,
            operation_id=paused_op,
            key="leftover",
            created_at=base + timedelta(seconds=1),
        )
    )

    broken_op = uuid.uuid4().hex
    session.add(
        _trace(
            user_id=user_id,
            operation_id=broken_op,
            started_at=base,
            ended_at=base + timedelta(seconds=2),
            status="error",
            agent_name="op-cs",
        )
    )
    session.add(
        _approval(
            user_id=user_id,
            operation_id=broken_op,
            key="broken",
            created_at=base + timedelta(seconds=1),
        )
    )
    await session.commit()

    async with await _client_for(seeded_identity["subject"]) as http:
        paused = (await http.get(f"/api/v1/observability/operations/{paused_op}")).json()
        broken = (await http.get(f"/api/v1/observability/operations/{broken_op}")).json()

    assert paused["display_state"] == "input-required"
    assert paused["attention"] is False
    # 单子还挂着 → 这次运行没有结束，历时算到此刻。
    assert paused["wall_ms"] >= 9 * 60 * 1000
    # 等待是历时的一部分，不会比它长。
    assert paused["waiting_ms"] <= paused["wall_ms"]

    assert broken["display_state"] == "failed"
    assert broken["attention"] is True
    assert broken["attention_reason"] == "pending_approval"


async def test_operation_is_404_for_someone_elses_run(op_env, session, seeded_identity):
    """别人的运行：404，不是 403。

    403 等于告诉对方「这个 id 是存在的」——`operation_id` 只用于分组，
    不是权限边界，边界在线上的 `user_id` 收敛那一步。
    """
    operation_id = uuid.uuid4().hex
    base = datetime.now(UTC) - timedelta(minutes=5)
    session.add(
        _trace(
            user_id="op-somebody-else",
            operation_id=operation_id,
            started_at=base,
            ended_at=base + timedelta(seconds=2),
            status="ok",
            agent_name="op-cs",
        )
    )
    await session.commit()

    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get(f"/api/v1/observability/operations/{operation_id}")

    assert response.status_code == 404

    # 有 observability:read 的身份看得到——收敛发生在出口，不是数据被改过。
    async with await _client_for(READER_SUBJECT) as http:
        assert (await http.get(f"/api/v1/observability/operations/{operation_id}")).status_code == 200


async def test_operation_rejects_unknown_id(op_env):
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get(f"/api/v1/observability/operations/{uuid.uuid4().hex}")

    assert response.status_code == 404
