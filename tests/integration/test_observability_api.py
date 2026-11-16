# tests/integration/test_observability_api.py
"""观测查询接口（M2）。

要守住的三条边界，比「接口能返回数据」更重要：

1. 没有 `observability:read` 的成员只能看到自己的 trace；
2. 别人的 trace_id 拿到也是 404，不是 403；
3. 列表接口永远不带原文，原文只在详情里出现。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.main import app
from app.models.agent import Agent, AgentStatus
from app.models.approval import ApprovalRequest, ApprovalStatus, RiskLevel
from app.models.audit import AuditEventType, AuditLog
from app.models.observability import ObsSpan, ObsTrace
from app.models.rbac import Role, RolePermission, UserRole

TENANT = "default"
PLAIN_SUBJECT = "obs-plain"
READER_SUBJECT = "obs-reader"
STRANGER_ID = "somebody-else"


async def _client_for(subject: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": TENANT,
        },
    )


async def _ensure_role(session, name: str, permissions: tuple[str, ...]) -> Role:  # noqa: ANN001
    role = await session.scalar(select(Role).where(Role.name == name))
    if role is None:
        role = Role(name=name, description=name)
        session.add(role)
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))
        await session.flush()
    return role


async def _ensure_member(session, *, subject: str, tenant_id: str, role: Role):  # noqa: ANN001
    from app.models.rbac import User

    user = await session.scalar(select(User).where(User.external_subject == subject))
    if user is None:
        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()

    exists = await session.scalar(
        select(UserRole).where(
            UserRole.tenant_id == tenant_id, UserRole.user_id == user.id
        )
    )
    if exists is None:
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role_id=role.id))
        await session.flush()
    return user


def _trace(
    *,
    user_id: str,
    started_at: datetime,
    status: str = "ok",
    intent: str = "chat",
    tool_name: str | None = None,
    agent_name: str | None = None,
    duration_ms: int = 1200,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    input_text: str = "今天周几？",
    output_text: str = "周一。",
) -> ObsTrace:
    return ObsTrace(
        tenant_id=TENANT,
        trace_id=uuid.uuid4().hex,
        user_id=user_id,
        conversation_id=None,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        status=status,
        error_category="tool_error" if status == "error" else None,
        intent=intent,
        tool_name=tool_name,
        agent_name=agent_name,
        span_count=3,
        llm_calls=1,
        tool_calls=1 if tool_name else 0,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_text=input_text,
        output_text=output_text,
        attrs={"model": "test-model"},
    )


@pytest.fixture
async def obs_fixture(session, seeded_identity):
    """两个身份（有/无 observability:read）+ 一份跨用户的 trace 数据集。"""
    # 测试库是 session 级的，观测表不会自动回滚；先清空，
    # 否则上一个用例的 trace 会被算进这个用例的总数里。
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))

    plain_role = await _ensure_role(session, "obs_plain", ("tools:read",))
    reader_role = await _ensure_role(
        session, "obs_reader", ("tools:read", "observability:read")
    )

    plain = await _ensure_member(
        session, subject=PLAIN_SUBJECT, tenant_id=TENANT, role=plain_role
    )
    await _ensure_member(
        session, subject=READER_SUBJECT, tenant_id=TENANT, role=reader_role
    )

    now = datetime.now(UTC)
    mine = _trace(user_id=str(plain.id), started_at=now - timedelta(minutes=10), tool_name="get_time")
    others = _trace(
        user_id=STRANGER_ID,
        started_at=now - timedelta(minutes=5),
        status="error",
        input_text="帮我查一下昨天那笔工单",
        output_text="查询失败。",
        duration_ms=4000,
    )
    stale = _trace(user_id=str(plain.id), started_at=now - timedelta(days=30))

    session.add_all([mine, others, stale])
    await session.flush()

    session.add_all(
        [
            ObsSpan(
                tenant_id=TENANT,
                trace_id=mine.trace_id,
                span_id="a" * 16,
                parent_span_id=None,
                seq=0,
                name="graph.invoke",
                kind="graph",
                started_at=now - timedelta(minutes=10),
                duration_ms=1200,
                status="ok",
                content='{"input": "今天周几？"}',
                attrs={},
            ),
            ObsSpan(
                tenant_id=TENANT,
                trace_id=mine.trace_id,
                span_id="b" * 16,
                parent_span_id="a" * 16,
                seq=1,
                name="tool.call",
                kind="tool",
                started_at=now - timedelta(minutes=10) + timedelta(milliseconds=100),
                duration_ms=8,
                status="ok",
                content='{"input": {"timezone": "Asia/Shanghai"}}',
                attrs={"tool": "get_time"},
            ),
            ObsSpan(
                tenant_id=TENANT,
                trace_id=others.trace_id,
                span_id="c" * 16,
                parent_span_id=None,
                seq=0,
                name="graph.invoke",
                kind="graph",
                started_at=now - timedelta(minutes=5),
                duration_ms=4000,
                status="error",
                error_category="tool_error",
                content='{"error": "timeout"}',
                attrs={},
            ),
        ]
    )
    await session.commit()

    yield {"plain_id": str(plain.id), "mine": mine.trace_id, "others": others.trace_id}

    # 走完必须清干净：同一个 sqlite 文件是整个 session 共用的，
    # 留下的 trace 会被 test_observability_chat 当成它自己那轮的结果取走。
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.commit()


async def test_plain_member_only_sees_own_traces(obs_fixture):
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get("/api/v1/observability/traces", params={"hours": 24})
    assert response.status_code == 200

    body = response.json()
    assert body["scope"] == "self"
    assert body["total"] == 1
    assert [item["trace_id"] for item in body["items"]] == [obs_fixture["mine"]]
    # 列表不带原文：几万字符的 prompt 一页 50 条就是几 MB。
    assert "input_text" not in body["items"][0]


async def test_reader_sees_whole_tenant_and_can_narrow(obs_fixture):
    async with await _client_for(READER_SUBJECT) as http:
        everyone = await http.get("/api/v1/observability/traces", params={"hours": 24})
        one = await http.get(
            "/api/v1/observability/traces",
            params={"hours": 24, "user_id": obs_fixture["plain_id"]},
        )

    assert everyone.json()["scope"] == "tenant"
    assert everyone.json()["total"] == 2
    # 30 天前那条在默认窗口外（保留期 14 天），不该被翻出来。
    assert all(
        item["trace_id"] != "stale" for item in everyone.json()["items"]
    )
    assert one.json()["total"] == 1


async def test_plain_member_gets_404_on_someone_elses_trace(obs_fixture):
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get(
            f"/api/v1/observability/traces/{obs_fixture['others']}"
        )
    # 403 等于告诉对方「这个 id 存在」。
    assert response.status_code == 404


async def test_detail_returns_spans_in_seq_order(obs_fixture):
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get(
            f"/api/v1/observability/traces/{obs_fixture['mine']}"
        )
        lean = await http.get(
            f"/api/v1/observability/traces/{obs_fixture['mine']}",
            params={"include_content": "false"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["trace"]["trace_id"] == obs_fixture["mine"]
    assert body["input_text"] == "今天周几？"
    assert [span["name"] for span in body["spans"]] == ["graph.invoke", "tool.call"]
    # 父子关系要能直接建树，前端不猜层级。
    assert body["spans"][1]["parent_span_id"] == body["spans"][0]["span_id"]
    assert body["spans"][1]["attrs"]["tool"] == "get_time"
    assert body["spans"][1]["content"]

    # 只看结构时不要把原文一起拉下来。
    assert lean.json()["spans"][0]["content"] is None


async def test_list_filters(obs_fixture):
    async with await _client_for(READER_SUBJECT) as http:
        by_status = await http.get(
            "/api/v1/observability/traces", params={"hours": 24, "status": "error"}
        )
        by_tool = await http.get(
            "/api/v1/observability/traces", params={"hours": 24, "tool_name": "get_time"}
        )
        by_text = await http.get(
            "/api/v1/observability/traces", params={"hours": 24, "q": "工单"}
        )
        by_window = await http.get(
            "/api/v1/observability/traces", params={"hours": 1, "status": "error"}
        )

    assert [i["trace_id"] for i in by_status.json()["items"]] == [obs_fixture["others"]]
    assert [i["trace_id"] for i in by_tool.json()["items"]] == [obs_fixture["mine"]]
    assert [i["trace_id"] for i in by_text.json()["items"]] == [obs_fixture["others"]]
    # 5 分钟前的那条落在 1 小时窗内；30 天前那条任何时候都不该出现。
    assert by_window.json()["total"] == 1


async def test_overview_aggregates(obs_fixture):
    async with await _client_for(READER_SUBJECT) as http:
        response = await http.get("/api/v1/observability/overview", params={"hours": 24})

    assert response.status_code == 200
    body = response.json()
    assert body["traces"] == 2
    assert body["errors"] == 1
    assert body["error_rate"] == 0.5
    assert body["llm_calls"] == 2
    assert body["prompt_tokens"] == 200
    assert body["max_duration_ms"] == 4000
    assert body["avg_duration_ms"] == 2600
    assert body["last_error_at"] is not None
    assert {b["key"] for b in body["by_status"]} == {"ok", "error"}
    assert body["top_tools"][0]["tool_name"] == "get_time"


async def test_overview_buckets_cover_the_window(obs_fixture):
    """概览的走势分格。

    守三条：格子加起来等于总量（否则柱子高度和左边的数字对不上）、
    格宽合法、最旧一格只有在被窗口起点截断时才标 partial。
    """
    async with await _client_for(READER_SUBJECT) as http:
        day = await http.get("/api/v1/observability/overview", params={"hours": 24})
        # 29 小时不是格宽的整数倍：最旧那一格会有一截落在窗口之外。
        odd = await http.get("/api/v1/observability/overview", params={"hours": 29})

    body = day.json()
    assert body["bucket_hours"] == 1
    assert len(body["buckets"]) == 24
    # 分格之和必须回到总数：加起来不平的图，读的人第一反应是数据错了。
    assert sum(b["traces"] for b in body["buckets"]) == body["traces"]
    assert sum(b["errors"] for b in body["buckets"]) == body["errors"]
    assert all(b["partial"] is False for b in body["buckets"])

    odd_body = odd.json()
    assert odd_body["bucket_hours"] == 3
    assert sum(b["traces"] for b in odd_body["buckets"]) == odd_body["traces"]
    # 截断只会发生在最旧的那一格，别处标了就是把「半格」说成了「整格」。
    assert odd_body["buckets"][0]["partial"] is True
    assert all(b["partial"] is False for b in odd_body["buckets"][1:])


async def test_awaiting_approval_is_not_counted_as_an_error(obs_fixture, session):
    """停在等人确认，不是失败。

    这条口径比任何数字都重要：`awaiting_approval` 是**设计好的暂停**——
    单子挂在审批页上，确认之后由**另一条** trace 接着执行（同一个
    `operation_id`）。把它算成错误，等于给每一次正常的写操作记一笔失败，
    于是有写操作的系统常年三成以上错误率，而那个数字不再指向任何要修的东西。

    反向验证：把 `CONCLUSIONLESS_STATUSES` 里的 `awaiting_approval` 删掉，
    `errors` 会变成 4、`error_rate` 变成 0.6667，这条立刻变红。
    """
    now = datetime.now(UTC)
    extra = [
        _trace(
            user_id=STRANGER_ID,
            started_at=now - timedelta(minutes=4),
            status="awaiting_approval",
            agent_name="obs-wait",
        ),
        _trace(
            user_id=STRANGER_ID,
            started_at=now - timedelta(minutes=3),
            status="awaiting_approval",
            agent_name="obs-wait",
        ),
        # 在跑与残行放在同一个用例里：「摘掉了哪些、留下了哪些」一次看全。
        # 前者没有结论，后者是事故，两者不该被同一条规则处理。
        _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=2), status="running"),
        _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=1), status="abandoned"),
    ]
    session.add_all(extra)
    await session.commit()

    try:
        async with await _client_for(READER_SUBJECT) as http:
            overview = await http.get("/api/v1/observability/overview", params={"hours": 24})
            agents = await http.get("/api/v1/observability/agents", params={"hours": 24})

        body = overview.json()
        # 6 轮 = 夹具的 ok / error 各一条 + 2 等人工 + 1 在跑 + 1 残行。
        assert body["traces"] == 6
        # 只剩真错误：夹具那条 error，加一条 abandoned（跑到一半进程没了）。
        assert body["errors"] == 2
        assert body["error_rate"] == 0.3333
        # 摘出去的那些必须有地方可去，否则界面上像是「凭空少了四轮」。
        assert body["awaiting_approval"] == 2
        by_status = {b["key"]: b["count"] for b in body["by_status"]}
        assert by_status["awaiting_approval"] == 2
        assert by_status["running"] == 1
        assert by_status["abandoned"] == 1
        # 最后一条错误的时间不能被一次「等人工」顶掉，否则「昨天开始报错了吗」
        # 会被一个没人点确认的单子写成「刚刚才报错」。
        assert body["last_error_at"] is not None

        wait = next(a for a in agents.json()["agents"] if a["agent_name"] == "obs-wait")
        assert wait["traces"] == 2
        assert wait["errors"] == 0
        assert wait["error_rate"] == 0.0
        assert wait["awaiting_approval"] == 2
    finally:
        # 测试库是会话级的：不清掉，后面按窗口统计的用例会跟着漂。
        for row in extra:
            await session.delete(row)
        await session.commit()


async def test_overview_marks_pipeline_off(session, seeded_identity, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "obs_enabled", False)
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get("/api/v1/observability/overview")

    # 开关关掉时界面要能说清楚「不是坏了，是没开」，而不是显示 0 条。
    assert response.status_code == 200
    assert response.json()["enabled"] is False


async def test_window_rejects_reversed_range(obs_fixture):
    now = datetime.now(UTC)
    async with await _client_for(PLAIN_SUBJECT) as http:
        response = await http.get(
            "/api/v1/observability/traces",
            params={
                "from": (now - timedelta(hours=1)).isoformat(),
                "to": (now - timedelta(hours=2)).isoformat(),
            },
        )
    assert response.status_code == 400


async def test_traces_expose_and_filter_by_agent(session, obs_fixture):
    """「谁在做事」要能从接口读出来，而不只是躺在库里。

    这个字段是给后面的「按 Agent 看绩效」铺路的：写进去却没有任何接口能
    读出来，等于没沉淀。存量行的 `agent_name` 是 NULL（那时还没有这个概念），
    所以按具体名字筛时不会把它们算进来——这是刻意的，不回溯编造。
    """
    now = datetime.now(UTC)
    session.add_all(
        [
            _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=3), agent_name="ops"),
            _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=2), agent_name="docs"),
        ]
    )
    await session.commit()  # 接口用独立 session，必须提交才看得见

    async with await _client_for(READER_SUBJECT) as http:
        all_rows = await http.get("/api/v1/observability/traces", params={"limit": 200})
        only_ops = await http.get(
            "/api/v1/observability/traces", params={"agent": "ops", "limit": 200}
        )

    assert all_rows.status_code == 200
    names = {item["agent_name"] for item in all_rows.json()["items"]}
    assert {"ops", "docs"} <= names
    # 存量行没有 agent_name：接口必须原样返回 null，而不是填个默认值糊过去。
    assert None in names

    filtered = only_ops.json()
    assert filtered["total"] == 1
    assert [item["agent_name"] for item in filtered["items"]] == ["ops"]


# ---------------------------------------------------------------------------
# 按智能体的运算基准
# ---------------------------------------------------------------------------


def _approval(
    *,
    user_id: str,
    agent_name: str | None,
    status: ApprovalStatus,
    created_at: datetime,
    approved_at: datetime | None = None,
) -> ApprovalRequest:
    """一张最小可用的审批单：只为按 agent_name 分组统计服务。"""
    return ApprovalRequest(
        tenant_id=TENANT,
        user_id=user_id,
        thread_id="thread-" + uuid.uuid4().hex[:8],
        run_id="run-" + uuid.uuid4().hex[:8],
        agent_name=agent_name,
        tool_name="create_maintenance_ticket",
        tool_display_name="创建检修工单",
        risk_level=RiskLevel.WRITE,
        status=status,
        reason="按智能体基准的测试数据",
        parameters_json={},
        parameter_hash="0" * 64,
        idempotency_key=uuid.uuid4().hex,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=10),
        approved_at=approved_at,
    )


def _agent_span(*, trace: ObsTrace, agent_name: str, started_at: datetime) -> ObsSpan:
    """一次委派在被委派方账上留下的那一个 span。

    只有委派会写 `kind=agent`（`delegate_tools` 里那层 `agent.{target}`），
    所以它同时是「谁被叫去干活」的唯一证据来源。
    """
    return ObsSpan(
        tenant_id=TENANT,
        trace_id=trace.trace_id,
        span_id=uuid.uuid4().hex[:16],
        name=f"agent.{agent_name}",
        kind="agent",
        agent_name=agent_name,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=800),
        duration_ms=800,
        status="ok",
        attrs={"agent": agent_name, "depth": 1},
    )


def _denial(*, actor_id: str, agent_name: str | None, created_at: datetime) -> AuditLog:
    return AuditLog(
        tenant_id=TENANT,
        actor_id=actor_id,
        event_type=AuditEventType.TOOL_DENIED,
        success=False,
        message="当前智能体未被授予该工具。",
        metadata_json={
            "tool_name": "create_maintenance_ticket",
            "denial_reason": "agent",
            "agent_name": agent_name,
            "agent_revision": 1,
        },
        created_at=created_at,
    )


@pytest.fixture
async def agent_env(session, obs_fixture):
    """干净的观测 / 审计 / 审批数据，外加三个 Agent 定义。

    `obs_fixture` 建好两个身份（有 / 无 `observability:read`）并清观测表；
    这里把它预置的 trace 也清掉，换成只在「按智能体」这组用例里有意义的样本。
    审计与审批表没有 session 级清理，必须由这个 fixture 兜住——否则我写进去的
    `tool_denied` 会被后面的用例当成它们自己造的事件。
    """
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.execute(delete(ApprovalRequest))
    await session.execute(delete(AuditLog))
    await session.execute(
        delete(Agent).where(
            Agent.name.in_(("obs-ops", "obs-docs", "obs-ghost", "obs-tech"))
        )
    )

    plain_id = obs_fixture["plain_id"]
    now = datetime.now(UTC)

    session.add_all(
        [
            Agent(
                tenant_id=TENANT,
                name="obs-ops",
                display_name="运行值班",
                description="负责站点运行问答。",
                status=AgentStatus.ACTIVE,
                revision=3,
            ),
            Agent(
                tenant_id=TENANT,
                name="obs-docs",
                display_name="规程问答",
                description="负责运行规程问答。",
                status=AgentStatus.DRAFT,
            ),
        ]
    )

    session.add_all(
        [
            # 运行值班：两轮，其中一轮失败。
            _trace(
                user_id=STRANGER_ID,
                started_at=now - timedelta(minutes=20),
                agent_name="obs-ops",
                duration_ms=1000,
            ),
            _trace(
                user_id=STRANGER_ID,
                started_at=now - timedelta(minutes=18),
                agent_name="obs-ops",
                status="error",
                duration_ms=3000,
            ),
            # 规程问答：一轮，草稿态也在用。
            docs_trace := _trace(
                user_id=STRANGER_ID,
                started_at=now - timedelta(minutes=16),
                agent_name="obs-docs",
                duration_ms=500,
            ),
            # 已经删掉的 Agent：历史行还在，必须仍能统计出来。
            _trace(
                user_id=STRANGER_ID,
                started_at=now - timedelta(minutes=14),
                agent_name="obs-ghost",
                duration_ms=700,
            ),
            # 存量行：那时还没有 agent 这个概念。
            _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=12)),
            _trace(user_id=STRANGER_ID, started_at=now - timedelta(minutes=10)),
            # 无 observability:read 的成员自己的一轮。委派的活儿也在这一轮里，
            # 用来验证「只看自己」时被委派的次数也跟着收敛。
            plain_trace := _trace(
                user_id=plain_id,
                started_at=now - timedelta(minutes=8),
                agent_name="obs-ops",
                duration_ms=1200,
            ),
        ]
    )

    # 「被委派」与「轮次」是两个数：obs-tech 从来没有被直接对话选中，
    # 它只出现在别人那一轮的 span 里。并集漏掉它，这一列就永远是 0。
    session.add_all(
        [
            _agent_span(
                trace=docs_trace,
                agent_name="obs-tech",
                started_at=now - timedelta(minutes=16),
            ),
            _agent_span(
                trace=plain_trace,
                agent_name="obs-tech",
                started_at=now - timedelta(minutes=8),
            ),
        ]
    )

    session.add_all(
        [
            _denial(actor_id=STRANGER_ID, agent_name="obs-ops", created_at=now - timedelta(minutes=17)),
            _denial(actor_id=STRANGER_ID, agent_name="obs-ops", created_at=now - timedelta(minutes=16)),
            # 没有 agent_name 的拒绝（不经对话链路的直接调用）：不计入任何一行。
            _denial(actor_id=STRANGER_ID, agent_name=None, created_at=now - timedelta(minutes=15)),
            _approval(
                user_id=STRANGER_ID,
                agent_name="obs-ops",
                status=ApprovalStatus.REJECTED,
                created_at=now - timedelta(minutes=17),
            ),
            _approval(
                user_id=STRANGER_ID,
                agent_name="obs-ops",
                status=ApprovalStatus.VERIFIED,
                created_at=now - timedelta(minutes=16),
            ),
            _approval(
                user_id=STRANGER_ID,
                agent_name="obs-docs",
                status=ApprovalStatus.PENDING,
                created_at=now - timedelta(minutes=15),
            ),
        ]
    )
    await session.commit()

    yield {"plain_id": plain_id}

    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.execute(delete(ApprovalRequest))
    await session.execute(delete(AuditLog))
    await session.execute(
        delete(Agent).where(
            Agent.name.in_(("obs-ops", "obs-docs", "obs-ghost", "obs-tech"))
        )
    )


async def test_agent_baseline_splits_by_agent(agent_env):
    """一个 Agent 的用量、错误率、耗时、被拒、审批要在同一行里看完。"""
    async with await _client_for(READER_SUBJECT) as http:
        response = await http.get("/api/v1/observability/agents", params={"hours": 24})

    assert response.status_code == 200
    body = response.json()

    assert body["total_traces"] == 7
    # 存量行单独一栏：塞进某个 Agent 就等于编造归属。
    assert body["unattributed_traces"] == 2
    # 被委派过的 Agent 也要露头：obs-tech 一次自己的轮次都没有，
    # 活儿全干在别人的 trace 里。它排在最后是因为先按轮次排。
    assert [a["agent_name"] for a in body["agents"]] == [
        "obs-ops",
        "obs-docs",
        "obs-ghost",
        "obs-tech",
    ]

    ops = body["agents"][0]
    assert ops["traces"] == 3
    assert ops["errors"] == 1
    assert ops["error_rate"] == 0.3333
    assert ops["share"] == 0.4286
    assert ops["avg_duration_ms"] == 1733  # (1000 + 3000 + 1200) / 3
    assert ops["p95_duration_ms"] == 3000
    assert ops["denials"] == 2
    assert ops["approvals"] == 2
    assert ops["approvals_rejected"] == 1
    # 定义侧的信息来自 agents 表，改动后立刻反映，不影响历史归属。
    assert ops["display_name"] == "运行值班"
    assert ops["status"] == "active"
    assert ops["revision"] == 3
    assert ops["last_started_at"] is not None
    # 运行值班自己没被叫过（它是发起方），这一列是 0 而不是空。
    assert ops["delegated_turns"] == 0


async def test_agent_baseline_marks_deleted_agent(agent_env):
    """库里已经没有这个 Agent，历史行照样统计，只是状态为 null。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (await http.get("/api/v1/observability/agents")).json()

    ghost = next(a for a in body["agents"] if a["agent_name"] == "obs-ghost")
    assert ghost["traces"] == 1
    assert ghost["status"] is None
    assert ghost["display_name"] is None
    # 没有报过单价就是 null，不是 0——0 会被读成「免费」。
    assert ghost["cost_micros"] is None


async def test_agent_baseline_counts_delegated_turns_separately(agent_env):
    """「被叫去干活」与「自己跑了几轮」是两个数，不能相加。

    obs-tech 有 0 轮自己的对话，却被叫了 2 次（一次在 obs-docs 的轮次里，
    一次在 plain 的轮次里）。并集漏掉它，这一列就永远是 0；
    把它并进 traces，`by_agent` 之和就不再等于总轮次。
    """
    async with await _client_for(READER_SUBJECT) as http:
        body = (await http.get("/api/v1/observability/agents", params={"hours": 24})).json()

    by_name = {a["agent_name"]: a for a in body["agents"]}
    assert by_name["obs-tech"]["delegated_turns"] == 2
    assert by_name["obs-tech"]["traces"] == 0

    # 发起方自己不会被记成「被委派」——它是在叫人的那一个。
    assert by_name["obs-docs"]["delegated_turns"] == 0
    assert body["total_traces"] == 7
    assert sum(a["traces"] for a in body["agents"]) + body["unattributed_traces"] == 7


async def test_agent_baseline_respects_self_scope(agent_env):
    """没有 observability:read 的成员只看得到自己的量，不是整个租户。"""
    async with await _client_for(PLAIN_SUBJECT) as http:
        body = (await http.get("/api/v1/observability/agents")).json()

    assert body["scope"] == "self"
    assert body["total_traces"] == 1
    assert body["unattributed_traces"] == 0
    by_name = {a["agent_name"]: a for a in body["agents"]}
    assert set(by_name) == {"obs-ops", "obs-tech"}
    assert by_name["obs-ops"]["share"] == 1.0
    # 别人的拒绝与审批不能漏到这个身份眼里。
    assert by_name["obs-ops"]["denials"] == 0
    assert by_name["obs-ops"]["approvals"] == 0
    # 委派的次数按**提要求的人**收敛：只数得到自己那一轮里的那一次。
    assert by_name["obs-tech"]["delegated_turns"] == 1
    assert by_name["obs-tech"]["traces"] == 0


def _delegated(*, actor_id: str, from_agent: str, to_agent: str, at: datetime) -> AuditLog:
    """一条委派审计。与 `delegate_tools._audit` 写进去的形状一致。"""
    return AuditLog(
        tenant_id=TENANT,
        actor_id=actor_id,
        event_type=AuditEventType.AGENT_DELEGATED,
        resource_type="agent",
        resource_id=to_agent,
        success=True,
        message="把活转交出去。",
        metadata_json={
            "via": "delegate_to_agent",
            "from_agent": from_agent,
            "to_agent": to_agent,
            "depth": 1,
            "status": "completed",
        },
        created_at=at,
    )


async def test_agent_baseline_reports_waiting_and_delegation_targets(session, obs_fixture):
    """两列新数各有各的坑，一起钉住。

    - `waiting_ms` 要按「区间与窗口有交集」取并裁剪：一张前天开、至今没人
      处理的单子，今天这段时间也一直在等人；而它落在窗口外的那两天不能算进来。
    - `delegated_to` 读审计，所以**被拒的尝试也算**，与按 span 数的
      `delegated_turns` 本来就不是一个数。这里故意造一次「有审计、没 span」，
      两个数字同时出现在一行里而互不相等——正是要让人看见的那件事。
    """
    plain_id = obs_fixture["plain_id"]
    now = datetime.now(UTC)
    marker = uuid.uuid4().hex[:8]
    busy = f"obs-wait-{marker}"
    stuck = f"obs-stuck-{marker}"

    session.add_all(
        [
            # 窗口内发起过一轮，所以它会出现在「按智能体」里。
            _trace(
                user_id=plain_id,
                started_at=now - timedelta(minutes=12),
                agent_name=busy,
            ),
            # 窗口内开的单子，两分钟就批了 → 等 120 秒。
            _approval(
                user_id=plain_id,
                agent_name=busy,
                status=ApprovalStatus.VERIFIED,
                created_at=now - timedelta(minutes=10),
                approved_at=now - timedelta(minutes=8),
            ),
            # 前天开、至今没人处理 → 只该贡献落在 24 小时窗口里的那一段。
            _approval(
                user_id=plain_id,
                agent_name=stuck,
                status=ApprovalStatus.PENDING,
                created_at=now - timedelta(days=2),
            ),
            _delegated(
                actor_id=plain_id,
                from_agent=busy,
                to_agent="obs-docs",
                at=now - timedelta(minutes=11),
            ),
        ]
    )
    await session.commit()

    try:
        async with await _client_for(PLAIN_SUBJECT) as http:
            body = (await http.get("/api/v1/observability/agents", params={"hours": 24})).json()

        rows = {a["agent_name"]: a for a in body["agents"]}
        assert rows[busy]["waiting_ms"] == pytest.approx(120_000, abs=2_000)
        # 有审计、没 span：两个数就是不一样，别拿它们对账。
        assert rows[busy]["delegated_turns"] == 0
        assert rows[busy]["delegated_to"] == [{"key": "obs-docs", "count": 1}]

        # 窗口外的等待被裁掉：只剩 24 小时那一段，不是两整天。
        assert rows[stuck]["waiting_ms"] == pytest.approx(24 * 3600 * 1000, abs=120_000)
    finally:
        await session.execute(delete(ObsSpan).where(ObsSpan.agent_name.in_((busy, stuck))))
        await session.execute(delete(ObsTrace).where(ObsTrace.agent_name.in_((busy, stuck))))
        await session.execute(
            delete(ApprovalRequest).where(ApprovalRequest.agent_name.in_((busy, stuck)))
        )
        await session.execute(
            delete(AuditLog).where(AuditLog.resource_id == "obs-docs", AuditLog.actor_id == plain_id)
        )
        await session.commit()


async def test_overview_by_agent_adds_up(agent_env):
    """概览的按智能体分布，各格之和必须等于总轮次。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (await http.get("/api/v1/observability/overview", params={"hours": 24})).json()

    buckets = {b["key"]: b["count"] for b in body["by_agent"]}
    assert buckets == {"obs-ops": 3, "—": 2, "obs-docs": 1, "obs-ghost": 1}
    assert sum(buckets.values()) == body["traces"] == 7


# ---------------------------------------------------------------------------
# 趋势：与上一个等长窗口相比
# ---------------------------------------------------------------------------


@pytest.fixture
async def trend_env(session, obs_fixture):
    """一份专门用来测对比的数据集：每个 Agent 覆盖一种「能不能比」的情形。

    `obs_fixture` 已经把角色和身份准备好了，这里借用它、再把观测表清成空，
    以免它预置的那几条 trace 混进窗口（其中一条在 30 天前，会落在上一窗口外）。
    """
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.execute(
        delete(Agent).where(
            Agent.name.in_(
                ("trend-big", "trend-flat", "trend-new", "trend-zero", "trend-tiny")
            )
        )
    )

    now = datetime.now(UTC)
    long_ago = now - timedelta(days=60)

    session.add_all(
        [
            Agent(tenant_id=TENANT, name=n, display_name=n, created_at=ts)
            for n, ts in (
                ("trend-big", long_ago),
                ("trend-flat", long_ago),
                # 两小时前才建：上一窗口开始时它还不存在。
                ("trend-new", now - timedelta(hours=2)),
                ("trend-zero", long_ago),
                ("trend-tiny", long_ago),
            )
        ]
    )

    def at(agent_name: str, offsets: list[timedelta], status: str = "ok") -> list[ObsTrace]:
        return [
            _trace(
                user_id=STRANGER_ID,
                started_at=now - delta,
                agent_name=agent_name,
                status=status,
            )
            for delta in offsets
        ]

    session.add_all(
        [
            # 当前 10 轮、上一窗口 5 轮：10 / 5 = +100%。
            # 位置刻意放在整点后 30 分钟，避开桶边界被请求时刻的毫秒数抖动影响。
            *at(
                "trend-big",
                [
                    timedelta(minutes=30),
                    timedelta(minutes=30),
                    timedelta(hours=3, minutes=30),
                    timedelta(hours=3, minutes=30),
                    timedelta(hours=3, minutes=30),
                    *[timedelta(hours=20)] * 5,
                ],
            ),
            *at("trend-big", [timedelta(hours=30)] * 5),
            # 6 → 6：完全持平，change 必须是 0.0 而不是 None。
            *at("trend-flat", [timedelta(hours=10)] * 6),
            *at("trend-flat", [timedelta(hours=40)] * 6),
            # 新建的 Agent：上一窗口 0 轮，但原因不是「没基线」而是「刚建」。
            *at("trend-new", [timedelta(hours=1, minutes=30)] * 6),
            # 60 天前就存在，上一窗口确实没有人用。
            *at("trend-zero", [timedelta(hours=5)] * 3),
            # 样本太小：2 → 3 只是同一个人的几次重试。
            *at("trend-tiny", [timedelta(hours=6)] * 3),
            *at("trend-tiny", [timedelta(hours=26)] * 2),
        ]
    )
    await session.commit()

    yield {"now": now}

    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    await session.execute(
        delete(Agent).where(
            Agent.name.in_(
                ("trend-big", "trend-flat", "trend-new", "trend-zero", "trend-tiny")
            )
        )
    )


async def test_agent_trend_splits_into_buckets(trend_env):
    """折线的每一格之和必须等于那一行的轮次——加不平就说明分桶漏了。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (
            await http.get("/api/v1/observability/agents", params={"hours": 24})
        ).json()

    assert body["bucket_hours"] == 1
    assert body["window"] is not None and body["previous"] is not None
    # 上一窗口紧挨着当前窗口，等长——否则比例没有意义。
    assert body["previous"]["end"] == body["window"]["start"]
    assert datetime.fromisoformat(body["window"]["end"]) - datetime.fromisoformat(
        body["window"]["start"]
    ) == timedelta(hours=24)
    assert datetime.fromisoformat(body["previous"]["end"]) - datetime.fromisoformat(
        body["previous"]["start"]
    ) == timedelta(hours=24)

    big = next(a for a in body["agents"] if a["agent_name"] == "trend-big")
    assert big["traces"] == 10
    assert len(big["buckets"]) == 24
    assert sum(b["traces"] for b in big["buckets"]) == big["traces"]

    # 最后一格收在「现在」，所以最近的用量落在最后一格，而不是第一格。
    assert big["buckets"][-1]["traces"] == 2
    assert big["buckets"][-4]["traces"] == 3
    assert big["buckets"][3]["traces"] == 5
    # 24 小时正好是 1 小时的整数倍，没有哪一格被窗口起点截断。
    assert not any(b["partial"] for b in big["buckets"])


async def test_agent_trend_reports_change_when_comparable(trend_env):
    async with await _client_for(READER_SUBJECT) as http:
        body = (
            await http.get("/api/v1/observability/agents", params={"hours": 24})
        ).json()

    big = next(a for a in body["agents"] if a["agent_name"] == "trend-big")
    assert big["previous_traces"] == 5
    assert big["change"] == 1.0
    assert big["change_reason"] is None

    flat = next(a for a in body["agents"] if a["agent_name"] == "trend-flat")
    assert flat["previous_traces"] == 6
    # 持平是 0.0，不是 None——「没变化」和「没法比」是两件事。
    assert flat["change"] == 0.0
    assert flat["change_reason"] is None


async def test_agent_trend_says_why_it_cannot_compare(trend_env):
    """三种不可比各自要说出原因，而不是统一填 0 或 null。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (
            await http.get("/api/v1/observability/agents", params={"hours": 24})
        ).json()

    by_name = {a["agent_name"]: a for a in body["agents"]}

    new = by_name["trend-new"]
    assert (new["change"], new["change_reason"]) == (None, "created_after_baseline_start")

    zero = by_name["trend-zero"]
    assert (zero["change"], zero["change_reason"]) == (None, "no_baseline")

    tiny = by_name["trend-tiny"]
    assert tiny["previous_traces"] == 2
    assert (tiny["change"], tiny["change_reason"]) == (None, "low_sample")


async def test_agent_trend_marks_clipped_oldest_bucket(trend_env):
    """窗口长度不是桶宽整数倍时，最旧那一格要标明只覆盖了一部分时间。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (
            await http.get(
                "/api/v1/observability/agents", params={"hours": 50, "bucket_hours": 3}
            )
        ).json()

    assert body["bucket_hours"] == 3
    big = next(a for a in body["agents"] if a["agent_name"] == "trend-big")
    assert len(big["buckets"]) == 17  # ceil(50 / 3)
    assert big["buckets"][0]["partial"] is True
    # 被截断的只可能是最旧那一格。
    assert not any(b["partial"] for b in big["buckets"][1:])


async def test_agent_trend_can_skip_comparison(trend_env):
    """不对比时 `previous` 为 null，而不是「上一窗口有 0 轮」那种伪数据。"""
    async with await _client_for(READER_SUBJECT) as http:
        body = (
            await http.get(
                "/api/v1/observability/agents",
                params={"hours": 24, "compare": "false"},
            )
        ).json()

    assert body["previous"] is None
    big = next(a for a in body["agents"] if a["agent_name"] == "trend-big")
    assert (big["change"], big["change_reason"]) == (None, None)
    # 但折线本身照旧——对比是可选的，趋势不是。
    assert len(big["buckets"]) == 24
