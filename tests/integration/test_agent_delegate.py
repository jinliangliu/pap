# tests/integration/test_agent_delegate.py
"""委派的接口层闭环：真的转交一次、真的拿到对方的结论、能力目录按调用者求交。

这里守的是「配置生效」与「不泄漏」两件事：
- 转交后发起方拿到的是**对方**的答案，不是自己编的；
- 能力目录里只能看到调用者本来就能看到的工具，否则它就是个情报接口。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.agents.schemas import ActionPlan
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.observability import ObsSpan
from app.observability.writer import ObsWriter
from app.tenancy.context import tenant_scope

TENANT = "default"


@pytest.fixture(autouse=True)
async def _clean_agents(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


async def _seed_agent(**kwargs) -> str:
    from app.services import agent_service

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                session,
                tenant_id=TENANT,
                name=kwargs.pop("name", "ops"),
                display_name=kwargs.pop("display_name", "运行值班"),
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                **kwargs,
            )
            await session.commit()
            return str(row.id)


async def test_chat_delegates_and_returns_the_specialist_answer(
    client, planner, seeded_identity, session
):
    """客服把问题转给运行值班，用户看到的是运行值班的结论。"""
    await _seed_agent(
        name="cs",
        display_name="客服",
        description="受理用户问题，不判断设备故障。",
        allowed_tools=["delegate_to_agent"],
        can_write=False,
    )
    await _seed_agent(
        name="ops",
        display_name="运行值班",
        description="设备故障排查与工单推进。",
        allowed_tools=None,
        can_write=False,
    )

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="delegate_to_agent",
                arguments={"target": "ops", "task": "查一下 3 号泵昨天为什么跳停"},
                reason="设备故障需要运行值班判断。",
            ),
            # 子调用里，运行值班先查一次再作答。
            # 这一步不是凑数：P1 起「没有任何依据的结论不准过桥」，
            # 一条凭空生成的答复会被拦下。
            ActionPlan(
                action="read_tool",
                tool_name="get_platform_time",
                reason="先确认当前时间，再对齐昨天的时间窗。",
            ),
            ActionPlan(action="chat", response="3 号泵昨日 14:20 压力越限触发保护停机。"),
            # 回到客服，客服转述。
            ActionPlan(action="chat", response="运行值班答复：3 号泵昨日 14:20 压力越限触发保护停机。"),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-delegate-ok",
            "message": "3 号泵昨天怎么了？",
            "agent": "cs",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "14:20" in body["answer"]

    # 委派留了账：接收方名字 + 发起方 + 深度，且不含任务原文。
    audits = (
        await session.execute(
            select(AuditLog).where(AuditLog.event_type == AuditEventType.AGENT_DELEGATED)
        )
    ).scalars().all()
    assert audits, "委派没有落审计"
    metadata = audits[-1].metadata_json
    assert metadata["from_agent"] == "cs"
    assert metadata["to_agent"] == "ops"
    assert metadata["depth"] == 1
    assert "3 号泵" not in str(metadata)


async def test_delegation_is_rejected_when_target_is_wider(
    client, planner, seeded_identity
):
    """只读客服不能把活交给一个更宽的智能体：拒绝理由要点名是哪一项。"""
    await _seed_agent(
        name="cs",
        display_name="客服",
        allowed_tools=["delegate_to_agent"],
        can_write=False,
    )
    await _seed_agent(
        name="ops",
        display_name="运行值班",
        allowed_tools=["get_platform_time", "search_knowledge"],
        can_write=False,
    )

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="delegate_to_agent",
                arguments={"target": "ops", "task": "查一下 3 号泵"},
                reason="交给运行值班。",
            ),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-delegate-wide",
            "message": "3 号泵昨天怎么了？",
            "agent": "cs",
        },
    )

    assert response.status_code == 200
    body = response.json()
    # 失败即收尾（不重试第二次），所以用户看到的就是拒绝理由本身。
    # 理由必须点到具体工具：只说「权限不足」会让人以为是人的授权问题，
    # 而真正该改的是被转交方的边界。
    assert "不能转交给" in body["answer"]
    assert "get_platform_time" in body["answer"]


async def test_capabilities_intersect_with_caller_visibility(client, seeded_identity):
    await _seed_agent(
        name="ops",
        display_name="运行值班",
        description="设备故障排查。",
        allowed_tools=["get_platform_time", "search_knowledge"],
    )

    response = await client.get("/api/v1/agents/ops/capabilities")
    assert response.status_code == 200

    data = response.json()
    assert data["agent"] == "ops"
    assert data["delegatable"] is True
    assert {tool["name"] for tool in data["tools"]} == {
        "get_platform_time",
        "search_knowledge",
    }
    # 职责说明是内部作业约束，不给别的 Agent 看。
    assert "instruction" not in data


async def test_capabilities_of_unrestricted_agent_only_lists_visible_tools(
    client, seeded_identity
):
    """allowed_tools=None 不等于「列出全部」：仍是调用者可见的那部分。

    构建助手的工具、以及租户禁用/角色不允许的工具都不该出现在这里。
    """
    await _seed_agent(name="ops", allowed_tools=None)

    response = await client.get("/api/v1/agents/ops/capabilities")
    names = {tool["name"] for tool in response.json()["tools"]}

    assert "get_platform_time" in names
    # 按调用者求交的证据：`studio_agent_save` 要 `agent:write`，tester 没有，
    # 所以它不该出现在这份目录里；而 `studio_describe` 读不设 scope，会出现。
    assert "studio_agent_save" not in names


async def test_capabilities_of_disabled_agent_explains_why(client, seeded_identity):
    await _seed_agent(name="ops", status=AgentStatus.DISABLED)

    data = (await client.get("/api/v1/agents/ops/capabilities")).json()
    assert data["delegatable"] is False
    assert "未启用" in data["delegatable_reason"]


async def test_capabilities_are_tenant_scoped(other_client, seeded_identity):
    await _seed_agent(name="ops")

    response = await other_client.get("/api/v1/agents/ops/capabilities")
    assert response.status_code == 404


async def _wait_for(condition, *, tries: int = 40, delay: float = 0.1):
    for _ in range(tries):
        result = await condition()
        if result:
            return result
        await asyncio.sleep(delay)
    return await condition()


async def test_delegation_leaves_one_agent_span_in_the_same_trace(
    client, planner, monkeypatch, seeded_identity
):
    """委派必须在**同一条** trace 里留下一个 `kind=agent` 的 span。

    两条 trace 会让「按 Agent 求和 == 总数」当场失效，而这类不一致最难被发现
    （`A2A.md` §10）。`delegated_turns` 这个指标就数这里的 span，所以先把它钉住。
    """
    await _seed_agent(name="cs", allowed_tools=["delegate_to_agent"], can_write=False)
    await _seed_agent(name="ops", description="设备故障排查。")

    writer = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", writer)
    monkeypatch.setattr(settings, "obs_enabled", True)

    await writer.start()
    try:
        planner(
            [
                ActionPlan(
                    action="read_tool",
                    tool_name="delegate_to_agent",
                    arguments={"target": "ops", "task": "查一下 3 号泵"},
                    reason="交给运行值班。",
                ),
                ActionPlan(
                    action="read_tool",
                    tool_name="get_platform_time",
                    reason="先确认时间窗。",
                ),
                ActionPlan(action="chat", response="3 号泵昨日跳停。"),
                ActionPlan(action="chat", response="运行值班说：3 号泵昨日跳停。"),
            ]
        )
        response = await client.post(
            "/api/v1/chat",
            json={
                "thread_id": "thread-delegate-span",
                "message": "3 号泵昨天怎么了？",
                "agent": "cs",
            },
        )
        assert response.status_code == 200

        async def load_spans():
            async with async_session_factory() as session:
                rows = (
                    await session.execute(select(ObsSpan).where(ObsSpan.kind == "agent"))
                ).scalars().all()
                return list(rows) if rows else None

        spans = await _wait_for(load_spans)
    finally:
        await writer.stop()

    assert spans, "委派没有留下 kind=agent 的 span"
    delegated = [span for span in spans if span.attrs.get("agent") == "ops"]
    assert len(delegated) == 1
    assert delegated[0].attrs["delegated_from"] == "cs"
    assert delegated[0].attrs["depth"] == 1


async def test_delegated_child_thread_is_not_left_behind(client, planner, seeded_identity):
    """子调用的检查点必须丢掉。

    它们是同步调用的副产物：结果已经回给发起方，那个 thread 没有恢复价值。
    留着的话，每个租户的 checkpoint 表里会攒下一堆不对应任何会话的孤立线程，
    会话清理也扫不到——属于「不会报错、只会一直长」的那类问题。
    """
    from app.db.checkpoint import get_checkpointer

    await _seed_agent(name="cs", allowed_tools=["delegate_to_agent"], can_write=False)
    await _seed_agent(name="ops")

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="delegate_to_agent",
                arguments={"target": "ops", "task": "查一下 3 号泵"},
                reason="交给运行值班。",
            ),
            ActionPlan(
                action="read_tool",
                tool_name="get_platform_time",
                reason="先确认时间窗。",
            ),
            ActionPlan(action="chat", response="跳停了。"),
            ActionPlan(action="chat", response="运行值班说跳停了。"),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-delegate-cleanup",
            "message": "3 号泵昨天怎么了？",
            "agent": "cs",
        },
    )
    assert response.status_code == 200

    storage = getattr(get_checkpointer(), "storage", {}) or {}
    leftovers = [key for key in storage if ":d1:ops:" in str(key)]
    assert leftovers == [], f"子调用线程没有被清理：{leftovers}"


async def test_delegate_with_refs_carries_materials_through_chat(
    client, planner, seeded_identity, session
):
    """接口层闭环：`refs` 从模型参数一路走到子调用，并在审计里留下 id。

    这里守的是「参数没在半路被丢掉」。`delegate_to_agent` 的 `refs` 是 P1 新加的，
    而它不在 `required` 里——漏接的实现照样能让所有 P0 用例通过，
    只是每次转交都「静默地少带了材料」。
    """
    await _seed_agent(name="cs", allowed_tools=["delegate_to_agent"], can_write=False)
    await _seed_agent(name="ops", allowed_tools=None, can_write=False)

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="delegate_to_agent",
                arguments={
                    "target": "ops",
                    "task": "按这份工单判断是否需要现场处置",
                    "refs": [{"kind": "entity", "id": "T002509008"}],
                },
                reason="需要运行值班按工单判断。",
            ),
            ActionPlan(action="chat", response="需要现场处置。"),
            ActionPlan(action="chat", response="运行值班答复：需要现场处置。"),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-delegate-refs",
            "message": "工单 T002509008 要不要派人去？",
            "agent": "cs",
        },
    )

    assert response.status_code == 200
    assert "现场处置" in response.json()["answer"]

    audits = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == AuditEventType.AGENT_DELEGATED,
                AuditLog.resource_id == "ops",
            )
        )
    ).scalars().all()

    assert audits, "委派没有落审计"
    assert audits[-1].metadata_json["refs"] == [{"kind": "entity", "id": "T002509008"}]
    # 审计只留 id：工单号本身是业务标识，可以留；正文不可以。
    assert "现场处置" not in str(audits[-1].metadata_json)
