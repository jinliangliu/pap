# tests/unit/test_agent_service.py
"""智能体边界：只收窄、不放宽，以及生命周期与默认 Agent 的幂等。

这组用例守的是四条最容易悄悄失守的约束：

1. **边界只收窄**：`allowed_tools` 只从「人本来能用的工具」里减，不会加；
2. **`can_write` 不是权限**：关掉后写类工具连目录都不出现，但打开也不豁免审批；
3. **`None` 与 `[]` 不同**：前者不收窄，后者一个都不给；
4. **默认 Agent 幂等**：不传 agent 的请求永远有落点，且不会建出两个。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.approval import RiskLevel
from app.services import agent_service
from app.services.agent_service import AgentBoundary, render_prompt_block
from app.tenancy.context import tenant_scope
from app.tools.gateway import ToolGateway
from app.tools.registry import ToolRegistry
from app.tools.types import ToolDefinition, ToolExecutionContext

TENANT = "default"


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    """`seeded_identity` 建出租户行（agents 有指向 tenants 的外键）。"""
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


async def _create(**kwargs):
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                session,
                tenant_id=TENANT,
                name=kwargs.pop("name", "ops"),
                display_name=kwargs.pop("display_name", "运行值班"),
                actor_user_id="actor-1",
                **kwargs,
            )
            await session.commit()
            return row


async def _get(name: str):
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            return await agent_service.get_agent(session, ref=name)


# ---------------- 默认 Agent ----------------


async def test_ensure_default_is_idempotent():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            first = await agent_service.ensure_default(session, tenant_id=TENANT)
            second = await agent_service.ensure_default(session, tenant_id=TENANT)
            await session.commit()

            assert first.id == second.id
            count = len(
                (await session.scalars(select(Agent).where(Agent.is_default.is_(True)))).all()
            )
            assert count == 1


async def test_default_agent_keeps_baseline_behaviour():
    """默认 Agent 不预填职责、可写、不收窄——等价于这套机制不存在。"""
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.ensure_default(session, tenant_id=TENANT)
            await session.commit()

    assert row.description == ""
    assert row.instruction == ""
    assert row.allowed_tools is None
    assert row.can_write is True

    boundary = agent_service.boundary_of(row)
    # 提示词块为空 = 不注入，升级不会改变既有会话的行为。
    assert render_prompt_block(boundary) == ""
    assert boundary.allows_tool("任意工具名") is True


async def test_baseline_boundary_is_permissive():
    boundary = agent_service.BASELINE
    assert boundary.allowed_tools is None
    assert boundary.can_write is True
    assert boundary.allows_tool("get_station_snapshot") is True
    assert boundary.allows_skill("任何技能") is True


# ---------------- 创建与校验 ----------------


async def test_new_agent_defaults_to_least_privilege():
    row = await _create()

    # 最小权限：新建的 Agent 默认只读、草稿态。要放权必须是显式动作。
    assert row.can_write is False
    assert row.status == AgentStatus.DRAFT
    assert row.is_default is False
    assert row.revision == 1


async def test_status_is_stored_as_enum_value_not_member_name():
    """落库的必须是小写 value（`active`），不是成员名（`ACTIVE`）。

    线上已经踩过：模型默认按成员名绑定，而迁移建的 PG 原生枚举
    `agent_status` 标签是小写 value，insert 直接
    `invalid input value for enum agent_status: "ACTIVE"` → 500。

    这条用例**读原始列值**，不读 ORM 属性：ORM 会把两种写法都还原成同一个
    枚举成员，永远看不出差别——必须在裸 SQL 这一层断言。
    """
    await _create(status=AgentStatus.ACTIVE)

    async with async_session_factory() as session:
        raw = await session.execute(
            text("SELECT status FROM agents WHERE name = :name"), {"name": "ops"}
        )
        stored = raw.scalar_one()

    assert stored == AgentStatus.ACTIVE.value


async def test_reserved_name_is_rejected():
    with pytest.raises(agent_service.AgentValidationError) as exc:
        await _create(name=agent_service.DEFAULT_AGENT_NAME)
    assert "保留" in str(exc.value)


async def test_duplicate_name_is_rejected():
    await _create(name="ops")
    with pytest.raises(agent_service.AgentValidationError) as exc:
        await _create(name="ops")
    assert "已被占用" in str(exc.value)


async def test_unknown_tool_is_rejected():
    with pytest.raises(agent_service.AgentValidationError) as exc:
        await _create(allowed_tools=["no-such-tool"])
    assert "未注册" in str(exc.value)


async def test_unknown_skill_is_rejected():
    with pytest.raises(agent_service.AgentValidationError) as exc:
        await _create(allowed_skills=["no-such-skill"])
    assert "技能不存在" in str(exc.value)


async def test_invalid_name_charset_is_rejected():
    with pytest.raises(agent_service.AgentValidationError):
        await _create(name="带空格 的名字")


# ---------------- 更新与版本 ----------------


async def test_revision_bumps_only_on_real_change():
    await _create(name="ops", description="负责运行规程问答")

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")
            # 值没变：不该让版本号虚增，否则「第 7 版边界」这种指认就失去意义。
            await agent_service.update_agent(
                session, agent=row, description="负责运行规程问答", actor_user_id="a"
            )
            assert row.revision == 1

            await agent_service.update_agent(
                session, agent=row, description="只管运行规程，不接工单", actor_user_id="a"
            )
            assert row.revision == 2
            await session.commit()


async def test_unset_keeps_value_but_explicit_none_widens():
    """`UNSET`（没传）保持原值；显式 `None` 表示取消收窄。两者必须区分。"""
    await _create(name="ops", allowed_tools=["get_station_snapshot"])

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")

            # 没传 allowed_tools → 保持收窄
            await agent_service.update_agent(
                session, agent=row, description="改说明", actor_user_id="a"
            )
            assert row.allowed_tools == ["get_station_snapshot"]

            # 显式 None → 取消收窄（这是一次明确的放宽，会 bump revision）
            before = row.revision
            await agent_service.update_agent(
                session, agent=row, allowed_tools=None, actor_user_id="a"
            )
            assert row.allowed_tools is None
            assert row.revision == before + 1
            await session.commit()


async def test_empty_allowed_tools_means_none_at_all():
    row = await _create(name="ops", allowed_tools=[])
    boundary = agent_service.boundary_of(row)

    assert boundary.allowed_tools == ()
    assert boundary.allows_tool("get_station_snapshot") is False
    # 与「不收窄」必须区分开
    assert agent_service.BASELINE.allows_tool("get_station_snapshot") is True


# ---------------- 生命周期 ----------------


async def test_default_agent_cannot_be_disabled_or_deleted():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.ensure_default(session, tenant_id=TENANT)
            await session.commit()

            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.set_status(session, agent=row, status=AgentStatus.DISABLED)
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.delete_agent(session, agent=row)


async def test_delete_requires_disabled_first():
    await _create(name="ops")

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")

            with pytest.raises(agent_service.AgentValidationError) as exc:
                await agent_service.delete_agent(session, agent=row)
            assert "先停用" in str(exc.value)

            await agent_service.set_status(session, agent=row, status=AgentStatus.DISABLED)
            await agent_service.delete_agent(session, agent=row)
            await session.commit()

    with pytest.raises(agent_service.AgentNotFound):
        await _get("ops")


# ---------------- 对话解析 ----------------


async def test_resolve_without_ref_falls_back_to_default():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            boundary = await agent_service.resolve_for_chat(session, tenant_id=TENANT, ref=None)
            await session.commit()

    assert boundary.is_default is True
    assert boundary.agent_name == agent_service.DEFAULT_AGENT_NAME


async def test_disabled_agent_is_not_usable_for_chat():
    await _create(name="ops", status=AgentStatus.ACTIVE)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")
            await agent_service.set_status(session, agent=row, status=AgentStatus.DISABLED)
            await session.commit()

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            with pytest.raises(agent_service.AgentUnavailable):
                await agent_service.resolve_for_chat(session, tenant_id=TENANT, ref="ops")


async def test_draft_agent_is_not_usable_for_chat():
    await _create(name="ops", status=AgentStatus.DRAFT)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            with pytest.raises(agent_service.AgentUnavailable):
                await agent_service.resolve_for_chat(session, tenant_id=TENANT, ref="ops")


async def test_unknown_agent_raises_not_found():
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            with pytest.raises(agent_service.AgentNotFound):
                await agent_service.resolve_for_chat(session, tenant_id=TENANT, ref="not-exist")


# ---------------- 提示词块 ----------------


def test_prompt_block_lists_restrictions():
    boundary = AgentBoundary(
        agent_name="ops",
        display_name="运行值班",
        instruction="只回答运行规程问题。",
        allowed_tools=("get_station_snapshot",),
        can_write=False,
    )
    block = render_prompt_block(boundary)

    assert "运行值班" in block
    assert "只回答运行规程问题。" in block
    assert "只能使用工具目录中列出的工具" in block
    assert "只读模式" in block


# ---------------- 网关强制执行 ----------------


def _tool(name: str, risk: RiskLevel) -> ToolDefinition:
    async def _handler(context, arguments):  # pragma: no cover - 不会被调用
        raise AssertionError("不应执行")

    return ToolDefinition(
        name=name,
        display_name=name,
        description=name,
        kind="python",
        risk_level=risk,
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        required_scopes=(),
    )


def _context(**kwargs) -> ToolExecutionContext:
    base = {
        "tenant_id": TENANT,
        "user_id": "u1",
        "thread_id": "t1",
        "run_id": "r1",
        "request_id": "req1",
        "roles": ("tester",),
        "scopes": ("tools:read",),
    }
    base.update(kwargs)
    return ToolExecutionContext(**base)


def test_gateway_denies_tool_outside_agent_allowlist():
    gateway = ToolGateway(ToolRegistry())
    tool = _tool("get_station_snapshot", RiskLevel.READ)

    allowed = gateway.agent_denial(
        tool=tool, context=_context(agent_allowed_tools=("get_station_snapshot",))
    )
    denied = gateway.agent_denial(tool=tool, context=_context(agent_allowed_tools=("other.tool",)))

    assert allowed is None
    assert denied is not None and "未被授予" in denied


def test_gateway_denies_write_tools_for_readonly_agent():
    gateway = ToolGateway(ToolRegistry())
    read_tool = _tool("get_station_snapshot", RiskLevel.READ)
    write_tool = _tool("create_maintenance_ticket", RiskLevel.WRITE)

    assert gateway.agent_denial(tool=read_tool, context=_context(agent_can_write=False)) is None

    denial = gateway.agent_denial(tool=write_tool, context=_context(agent_can_write=False))
    assert denial is not None and "只读" in denial

    # 打开 can_write 只是解除这一层；审批仍由 requires_approval 决定，不是豁免。
    assert gateway.agent_denial(tool=write_tool, context=_context(agent_can_write=True)) is None


def test_gateway_baseline_context_is_unrestricted():
    gateway = ToolGateway(ToolRegistry())
    write_tool = _tool("create_maintenance_ticket", RiskLevel.WRITE)

    # 不传 agent 相关字段 = 平台基线，行为与改造前一致。
    assert gateway.agent_denial(tool=write_tool, context=_context()) is None


# ---------------- 审批执行时的边界交集 ----------------


async def test_approval_boundary_narrows_but_never_widens():
    """审批时的快照与当前配置取交集：收窄立即生效，放宽不生效。

    这是「审批跨两次请求」带来的真实漏洞：一张按只读批准的写单，
    若中途把 Agent 放开成可写，只看当前配置就会照跑。
    """
    from types import SimpleNamespace

    from app.db.session import async_session_factory
    from app.services.approval_service import ApprovalService
    from app.tools.dependencies import get_tool_gateway

    service = ApprovalService(
        session_factory=async_session_factory,
        tool_gateway=get_tool_gateway(),
    )

    await _create(
        name="ops",
        status=AgentStatus.ACTIVE,
        allowed_tools=["get_station_snapshot"],
        can_write=False,
    )

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")
            await session.commit()

    # 快照更宽（两个工具 + 可写），当前更窄 → 取当前
    snapshot_wide = SimpleNamespace(
        agent_id=str(row.id),
        agent_name="ops",
        agent_allowed_tools=["get_station_snapshot", "create_maintenance_ticket"],
        agent_can_write=True,
        # 真实审批行上总有这一列（`NULL` = 非委派）。少了它，
        # `_execution_agent_boundary` 会在复核委派链时 AttributeError——
        # 桩要与真表同形，否则测的是桩的形状。
        delegation_chain=None,
    )
    tools, can_write = await service._execution_agent_boundary(snapshot_wide)
    assert tools == ("get_station_snapshot",)
    assert can_write is False

    # 快照更窄（一个工具 + 只读），当前更宽 → 仍取交集，不能放宽
    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")
            await agent_service.update_agent(
                session, agent=row, allowed_tools=None, can_write=True, actor_user_id="a"
            )
            await session.commit()

    snapshot_narrow = SimpleNamespace(
        agent_id=str(row.id),
        agent_name="ops",
        agent_allowed_tools=["get_station_snapshot"],
        agent_can_write=False,
        delegation_chain=None,
    )
    tools, can_write = await service._execution_agent_boundary(snapshot_narrow)
    assert tools == ("get_station_snapshot",)
    assert can_write is False


async def test_approval_boundary_rejects_disabled_agent():
    from types import SimpleNamespace

    from app.db.session import async_session_factory
    from app.services.approval_service import ApprovalService
    from app.tools.dependencies import get_tool_gateway

    service = ApprovalService(
        session_factory=async_session_factory,
        tool_gateway=get_tool_gateway(),
    )

    await _create(name="ops", status=AgentStatus.ACTIVE)

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(session, ref="ops")
            await agent_service.set_status(
                session, agent=row, status=AgentStatus.DISABLED
            )
            await session.commit()

    stale = SimpleNamespace(
        agent_id=str(row.id),
        agent_name="ops",
        agent_allowed_tools=None,
        agent_can_write=True,
        delegation_chain=None,
    )

    with pytest.raises(agent_service.AgentUnavailable):
        await service._execution_agent_boundary(stale)


# ---------------- 委派预算 ----------------
#
# 两件事：范围校验在服务层拦住（界面上失手不该变成一条脏数据），
# 以及「0」与「没配」必须能分开存取——0 是「不许转交」这个开关，
# 被读成「没配」就会静默回落到平台默认。


async def test_delegation_budget_out_of_range_is_rejected():
    with pytest.raises(agent_service.AgentValidationError):
        await _create(max_delegation_depth=6)
    with pytest.raises(agent_service.AgentValidationError):
        await _create(delegation_budget=-1)


async def test_delegation_budget_keeps_zero_and_null_apart():
    await _create(name="no-delegate", max_delegation_depth=0)
    row = await _get("no-delegate")
    assert row.max_delegation_depth == 0
    # 没配的那一项是 None，不是 0：前者走平台默认，后者是「一次都不许」。
    assert row.delegation_budget is None

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            stored = await agent_service.get_agent(session, ref="no-delegate")
            # 显式传 None = 回到平台默认。
            await agent_service.update_agent(session, agent=stored, max_delegation_depth=None)
            await session.commit()

    assert (await _get("no-delegate")).max_delegation_depth is None
