# tests/integration/test_obs_topology.py
"""运行视图 L2：委派拓扑。

这块最难发现的错误不是「图画不出来」，而是**图看起来对、边却连错了人**，
或者把「被拒」画成了「成功」——那会把人引去修一个配得好好的边界。
所以三条用例都对着一个具体的错误实现写，去掉实现必须变红：

1. **三种边分开。** 把 awaiting_approval 与 completed 合成一档，或者让被拒的
   转交不出边，第 1 条立刻红。
2. **配置了但没被调用的 Agent 也在图上（隔离、不带边）。** 只从边推节点，
   它就会凭空消失——而「建了没人用」正是这张图最值钱的答案之一。
3. **`self` 身份拿到空集而不是 403。** 边会暴露「别人在用哪个智能体」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.main import app
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.observability import ObsSpan, ObsTrace
from app.models.workflow import Workflow
from app.models.rbac import Role, RolePermission, UserRole

TENANT = "default"
PLAIN_SUBJECT = "topo-plain"
READER_SUBJECT = "topo-reader"


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


def _delegation(
    *,
    from_agent: str | None,
    to_agent: str,
    status: str,
    created_at: datetime,
    depth: int = 1,
    reason: str | None = None,
) -> AuditLog:
    """一条委派审计。字段与 `delegate_tools._audit` 写进去的一致。"""
    return AuditLog(
        tenant_id=TENANT,
        actor_id="topo-actor",
        event_type=AuditEventType.AGENT_DELEGATED,
        resource_type="agent",
        resource_id=to_agent,
        thread_id="thread-topo",
        success=status == "completed",
        message="测试委派",
        metadata_json={
            "via": "delegate_to_agent",
            "from_agent": from_agent,
            "to_agent": to_agent,
            "depth": depth,
            "status": status,
            "reject_reason": reason,
        },
        created_at=created_at,
    )


def _agent_span(*, trace_id: str, agent_name: str, parent: str | None, started_at: datetime, ms: int):
    return ObsSpan(
        tenant_id=TENANT,
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=None,
        seq=0,
        name=f"agent.{agent_name}",
        kind="agent",
        agent_name=agent_name,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=ms),
        duration_ms=ms,
        status="ok",
        attrs={"agent": agent_name, "depth": 1, "delegated_from": parent},
    )


async def _clean(session) -> None:  # noqa: ANN001
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))
    # 只删本文件写的那几条：审计表是会话级共用的，按事件类型一刀切
    # 会把别的用例（真实委派链路）的记账也清掉。
    await session.execute(delete(AuditLog).where(AuditLog.thread_id == "thread-topo"))
    await session.execute(
        delete(Agent).where(Agent.name.in_(("topo-cs", "topo-ops", "topo-docs", "topo-idle")))
    )
    await session.execute(delete(Workflow).where(Workflow.name.like("topo-wf%")))
    await session.commit()


@pytest.fixture
async def topo_env(session, seeded_identity):  # noqa: ANN001
    """两个身份：一个带 observability:read，一个不带。"""
    await _clean(session)

    plain_role = await _role(session, "topo_plain", ("tools:read",))
    reader_role = await _role(session, "topo_reader", ("tools:read", "observability:read"))
    await _member(session, subject=PLAIN_SUBJECT, role=plain_role)
    await _member(session, subject=READER_SUBJECT, role=reader_role)
    await session.commit()

    yield

    await _clean(session)


def _edges(body: dict) -> dict[tuple, dict]:
    """(from, to, kind) → 边。断言时直接按业务键取，不靠下标。"""
    return {(e["from_agent"], e["to_agent"], e["kind"]): e for e in body["edges"]}


async def test_topology_separates_success_paused_and_rejected(topo_env, session):
    """同一对 Agent 之间，三种结局必须是三条边。

    把「等人工」和「跑完」合成一档，或者让被拒的转交不出边，
    这张图就只剩「谁调用过谁」，回答不了「为什么那条路一直走不通」。
    """
    await _clean(session)
    now = datetime.now(UTC)

    session.add_all(
        [
            Agent(
                tenant_id=TENANT,
                name="topo-cs",
                display_name="客服",
                status=AgentStatus.ACTIVE,
                can_write=False,
            ),
            Agent(
                tenant_id=TENANT,
                name="topo-ops",
                display_name="运维",
                status=AgentStatus.ACTIVE,
                can_write=True,
            ),
            # 配好了，但窗口内一次都没被调用。
            Agent(
                tenant_id=TENANT,
                name="topo-idle",
                display_name="闲置专家",
                status=AgentStatus.ACTIVE,
            ),
        ]
    )
    session.add_all(
        [
            # cs → ops：跑通两次。
            _delegation(
                from_agent="topo-cs", to_agent="topo-ops", status="completed",
                created_at=now - timedelta(minutes=30),
            ),
            _delegation(
                from_agent="topo-cs", to_agent="topo-ops", status="completed",
                created_at=now - timedelta(minutes=20),
            ),
            # cs → ops：第三次停在审批上。同一对，不同结局。
            _delegation(
                from_agent="topo-cs", to_agent="topo-ops", status="awaiting_approval",
                created_at=now - timedelta(minutes=10),
            ),
            # cs → docs：边界太宽，被拒。这条边在 span 上根本不存在。
            _delegation(
                from_agent="topo-cs", to_agent="topo-docs", status="rejected",
                created_at=now - timedelta(minutes=5), reason="delegate_widens",
            ),
            # 平台基线（没有 agent_name）也在委派：from 为 None。
            _delegation(
                from_agent=None, to_agent="topo-ops", status="completed",
                created_at=now - timedelta(minutes=2),
            ),
        ]
    )

    # 耗时只从 span 取：两条跑通的转交，8 秒与 12 秒。
    trace_id = uuid.uuid4().hex
    session.add_all(
        [
            _agent_span(
                trace_id=trace_id, agent_name="topo-ops", parent="topo-cs",
                started_at=now - timedelta(minutes=30), ms=8000,
            ),
            _agent_span(
                trace_id=trace_id, agent_name="topo-ops", parent="topo-cs",
                started_at=now - timedelta(minutes=20), ms=12000,
            ),
        ]
    )
    await session.commit()

    try:
        async with await _client_for(READER_SUBJECT) as http:
            response = await http.get("/api/v1/observability/topology", params={"hours": 24})

        assert response.status_code == 200
        body = response.json()
        assert body["restricted"] is False
        edges = _edges(body)

        ok = edges[("topo-cs", "topo-ops", "ok")]
        assert ok["count"] == 2
        # 最近一次是「等人工」那条，不是这里的两条——两档各算各的。
        assert ok["p95_ms"] is not None
        assert ok["reasons"] == []

        paused = edges[("topo-cs", "topo-ops", "paused")]
        assert paused["count"] == 1
        # 暂停的还没跑完，拿它的耗时进 P95 是编数。
        assert paused["p95_ms"] is None

        rejected = edges[("topo-cs", "topo-docs", "rejected")]
        assert rejected["count"] == 1
        assert rejected["reasons"] == [{"key": "delegate_widens", "count": 1}]
        # 被拒的转交不能同时出现在「跑通」里。
        assert ("topo-cs", "topo-docs", "ok") not in edges

        # 平台基线单独一个节点：from 为 None，不能凭空消失。
        assert edges[(None, "topo-ops", "ok")]["count"] == 1
        assert any(node["name"] is None for node in body["nodes"])

        nodes = {node["name"]: node for node in body["nodes"]}

        # 被转交四次：cs 跑通两次 + 暂停一次，平台基线跑通一次。
        # 这个数含被拒的尝试（见 schema 里的说明），所以它不等于
        # `/agents` 里按 span 数的 delegated_turns。
        assert nodes["topo-ops"]["attempts"] == 4
        assert nodes["topo-ops"]["depth"] == 1
        assert nodes["topo-ops"]["can_write"] is True
        # 只有被拒的入边：仍然算「有人试图交给它」。
        assert nodes["topo-docs"]["attempts"] == 1
        # 只有被拒的入边，它仍然「被观测到」了：有人试图叫它。
        assert nodes["topo-docs"]["observed"] is True
        assert nodes["topo-cs"]["depth"] == 0
    finally:
        await _clean(session)


async def test_topology_lists_configured_agent_without_edges(topo_env, session):
    """配置了但从没被调用：出现在节点里，但没有任何边。

    只从边推节点集，这个 Agent 会凭空消失——而「我建的这个专家到底有没有用」
    正是这张图要回答的问题。去掉「配置侧取名字」这一步，这条立刻红。
    """
    await _clean(session)
    now = datetime.now(UTC)

    session.add_all(
        [
            Agent(
                tenant_id=TENANT,
                name="topo-cs",
                display_name="客服",
                status=AgentStatus.ACTIVE,
            ),
            Agent(
                tenant_id=TENANT,
                name="topo-idle",
                display_name="闲置专家",
                status=AgentStatus.DRAFT,
            ),
        ]
    )
    session.add(
        _delegation(
            from_agent="topo-cs", to_agent="topo-ops", status="completed",
            created_at=now - timedelta(minutes=10),
        )
    )
    await session.commit()

    try:
        async with await _client_for(READER_SUBJECT) as http:
            body = (
                await http.get("/api/v1/observability/topology", params={"hours": 24})
            ).json()

        nodes = {node["name"]: node for node in body["nodes"]}
        idle = nodes["topo-idle"]
        assert idle["configured"] is True
        assert idle["observed"] is False
        # 不在图上，也就没有「第几跳」可言。
        assert idle["depth"] is None
        assert idle["status"] == "draft"

        # 一条边都不许指向它。
        assert all(
            "topo-idle" not in (edge["from_agent"], edge["to_agent"]) for edge in body["edges"]
        )

        # 被调用过的那个反过来：配置里有、观测里也有。
        cs = nodes["topo-cs"]
        assert cs["configured"] is True
        assert cs["observed"] is True
    finally:
        await _clean(session)


async def test_topology_returns_empty_for_self_scope(topo_env, session):
    """没有 observability:read 的成员：空集 + 原因，不是 403。

    403 会把「你没权限看这张图」说成「这个接口坏了」；而空集加一句话，
    界面能直接显示「需要 observability:read」。边会暴露「别人在用哪个
    智能体」，所以这里连节点也不能给。
    """
    await _clean(session)
    now = datetime.now(UTC)

    session.add_all(
        [
            Agent(
                tenant_id=TENANT,
                name="topo-cs",
                display_name="客服",
                status=AgentStatus.ACTIVE,
            ),
        ]
    )
    session.add(
        _delegation(
            from_agent="topo-cs", to_agent="topo-ops", status="completed",
            created_at=now - timedelta(minutes=10),
        )
    )
    await session.commit()

    try:
        async with await _client_for(PLAIN_SUBJECT) as http:
            response = await http.get(
                "/api/v1/observability/topology", params={"hours": 24}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["restricted"] is True
        assert body["scope"] == "self"
        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["reason"]

        # 同一个租户里，有权限的身份看到的是同一张图——说明收敛发生在出口，
        # 不是数据被谁改过。
        async with await _client_for(READER_SUBJECT) as http:
            full = (await http.get("/api/v1/observability/topology", params={"hours": 24})).json()
        assert full["restricted"] is False
        assert full["edges"]
    finally:
        await _clean(session)


async def test_declared_edges_come_from_workflow_definitions(topo_env, session):  # noqa: ANN001
    """声明层：配了没跑过的边也在图上，跑过没声明的边标 `declared=false`。

    这两条是这张图唯一能校验配置的地方。只画实际（改造前的行为）时，
    「图里连了但边界把它全挡住了」永远看不出来。
    """
    from app.orchestration.definition import compile_definition

    plan = compile_definition(
        {
            "entry": "a",
            "nodes": [
                {"id": "a", "agent": "topo-cs", "task": "分类",
                 "next": [{"when": None, "to": "b"}]},
                {"id": "b", "agent": "topo-ops", "task": "处理", "next": []},
            ],
        }
    )

    session.add(
        Workflow(
            tenant_id=TENANT,
            name="topo-wf-triage",
            display_name="告警排查",
            revision=1,
            definition=plan.as_json(),
            enabled=True,
        )
    )
    # 一条实际发生、但不在任何流程里的边：模型自主委派。
    session.add(
        _delegation(
            from_agent="topo-docs",
            to_agent="topo-idle",
            status="completed",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    async with await _client_for(READER_SUBJECT) as client:
        response = await client.get("/api/v1/observability/topology")
        assert response.status_code == 200
        body = response.json()

    assert body["workflow_count"] == 1

    declared = {(item["from_agent"], item["to_agent"]): item for item in body["declared_edges"]}
    assert ("topo-cs", "topo-ops") in declared
    assert declared[("topo-cs", "topo-ops")]["workflows"] == ["topo-wf-triage"]
    # 这张流程还没被触发过。
    assert declared[("topo-cs", "topo-ops")]["observed"] is False

    edges = {(item["from_agent"], item["to_agent"]): item for item in body["edges"]}
    # 跑过但没声明 → 模型在自主路由。
    assert edges[("topo-docs", "topo-idle")]["declared"] is False
