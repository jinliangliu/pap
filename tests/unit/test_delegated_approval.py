# tests/unit/test_delegated_approval.py
"""委派审批的执行期复核：批准与执行之间，链上任何一跳变了都作废。

为什么值得单独一组用例：这条复核最容易被写成「只查 B 自己还可用吗」，
而那个版本在**所有单跳用例里都是绿的**——A 收窄时 B 一点没变。
A→B→C 这种两跳的链才是它真正的考点，所以这里必须有一条两跳的用例，
并且去掉逐跳循环（只比直接上级）时它必须红。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.approval import ApprovalRequest
from app.services import agent_service
from app.services.approval_service import ApprovalService, DelegationRevoked
from app.tenancy.context import tenant_scope
from app.tools.gateway import ToolGateway
from app.tools.registry import ToolRegistry

TENANT = "default"


@pytest.fixture(autouse=True)
async def _clean_agents(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


def _service() -> ApprovalService:
    return ApprovalService(
        session_factory=async_session_factory,
        tool_gateway=ToolGateway(ToolRegistry()),
        default_expire_seconds=300,
    )


async def _agent(name: str, *, tools=None, can_write: bool = False, **kwargs) -> str:
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                session,
                tenant_id=TENANT,
                name=name,
                display_name=name,
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                allowed_tools=tools,
                can_write=can_write,
                **kwargs,
            )
            await session.commit()
            return str(row.id)


async def _boundary(name: str):
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref=name)
            return agent_service.boundary_of(row)


def _approval(chain: list[dict]) -> ApprovalRequest:
    """只构造复核需要的那几个字段：方法本身不读库里的审批行。"""
    return ApprovalRequest(
        tenant_id=TENANT,
        user_id="u-1",
        thread_id="child-thread",
        run_id="",
        tool_name="create_maintenance_ticket",
        tool_display_name="创建维护工单",
        delegation_chain=chain,
    )


async def test_review_passes_when_every_hop_still_holds():
    a = await _agent("a", tools=["get_platform_time", "search_knowledge"], can_write=True)
    await _agent("b", tools=["get_platform_time"], can_write=True)

    await _service()._review_delegation(
        _approval([{"id": a, "name": "a"}]),
        await _boundary("b"),
    )


async def test_review_revokes_when_the_direct_parent_was_narrowed():
    a = await _agent("a", tools=["get_platform_time", "search_knowledge"], can_write=True)
    await _agent("b", tools=["get_platform_time"], can_write=True)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="a")
            await agent_service.update_agent(
                session, agent=row, allowed_tools=["search_knowledge"], actor_user_id="admin"
            )
            await session.commit()

    with pytest.raises(DelegationRevoked) as exc:
        await _service()._review_delegation(
            _approval([{"id": a, "name": "a"}]),
            await _boundary("b"),
        )

    # 理由要点名是哪个工具越界了，否则排障只知道「链失效」。
    assert "get_platform_time" in str(exc.value)


async def test_review_revokes_when_the_root_was_narrowed():
    """两跳的关键用例：B 完全没变，变的是更上面的 A。

    只复核直接上级的实现会在这里放行——而那张单子已经是
    「一次不该发生的委派的产物」的产物。
    """
    a = await _agent("a", tools=["get_platform_time", "search_knowledge"], can_write=True)
    b = await _agent("b", tools=["get_platform_time"], can_write=True)
    c = await _agent("c", tools=["get_platform_time"], can_write=True)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="a")
            await agent_service.update_agent(
                session, agent=row, allowed_tools=["search_knowledge"], actor_user_id="admin"
            )
            await session.commit()

    chain = [{"id": a, "name": "a"}, {"id": b, "name": "b"}]

    with pytest.raises(DelegationRevoked) as exc:
        await _service()._review_delegation(_approval(chain), await _boundary("c"))

    assert "a" in str(exc.value)
    assert c  # 只是为了说明「执行方自己是正常的那一个」


async def test_review_revokes_when_an_ancestor_was_deleted():
    a = await _agent("a")
    await _agent("b", tools=["get_platform_time"], can_write=True)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="a")
            await session.delete(row)
            await session.commit()

    with pytest.raises(DelegationRevoked) as exc:
        await _service()._review_delegation(
            _approval([{"id": a, "name": "a"}]),
            await _boundary("b"),
        )

    assert "已被删除" in str(exc.value)


async def test_review_revokes_when_an_ancestor_was_disabled():
    a = await _agent("a")
    await _agent("b", tools=["get_platform_time"], can_write=True)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="a")
            await agent_service.set_status(
                session, agent=row, status=AgentStatus.DISABLED
            )
            await session.commit()

    with pytest.raises(DelegationRevoked) as exc:
        await _service()._review_delegation(
            _approval([{"id": a, "name": "a"}]),
            await _boundary("b"),
        )

    assert "已停用" in str(exc.value)


async def test_review_skips_a_hop_without_an_id():
    """平台基线没有配置行：无从收窄，也就无从复核。不能因此拒绝。"""
    idless = {"id": None, "name": "平台默认智能体"}
    b = await _agent("b", tools=["get_platform_time"], can_write=True)
    assert b

    await _service()._review_delegation(_approval([idless]), await _boundary("b"))


async def test_review_does_nothing_for_a_non_delegated_approval():
    await _agent("b", tools=["get_platform_time"], can_write=True)
    await _service()._review_delegation(_approval([]), await _boundary("b"))
