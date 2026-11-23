# tests/unit/test_studio_tools.py
"""构建助手：内置身份、自提权防护与二次确认的前提。

这组用例守的是四条最容易悄悄失守的约束：

1. **内置 Agent 存在且边界固定**：它自己能改别人，别人不能改它；
2. **不能把 `studio_*` 授予别的 Agent**：那等于造一个绕过审批改配置的 Agent；
3. **授予的工具必须是申请人可见的**：否则配出来一条都调不动；
4. **删除要输对名字**：核对在工具内部做，不靠模型自觉。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.services import agent_service
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "roles": ("dev",),
        # dev 角色有 observability:read 与 tools:read；agent:write 由这里显式给。
        "scopes": ("tools:read", "agent:write"),
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


async def _ensure_studio():
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.ensure_studio(db, tenant_id=TENANT, actor_user_id="actor-1")
            await db.commit()
            return row


async def _create_agent(name: str, **kwargs):
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name=name,
                display_name=name,
                actor_user_id="actor-1",
                **kwargs,
            )
            await db.commit()
            return row


async def _get(name: str):
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            return await agent_service.get_agent(db, ref=name)


# ---------------- 内置身份 ----------------


async def test_studio_is_created_with_only_studio_tools():
    row = await _ensure_studio()

    assert row.is_builtin is True
    assert row.name == agent_service.STUDIO_AGENT_NAME
    assert row.status == AgentStatus.ACTIVE
    assert row.can_write is True
    # 只放行管理工具，外加明确列入 STUDIO_EXTRA_TOOLS 的读取类能力：
    # 放行再多就等于给了一个「不经过审批的写入口」。
    assert row.allowed_tools
    assert all(
        name.startswith("studio_") or name in agent_service.STUDIO_EXTRA_TOOLS
        for name in row.allowed_tools
    )


async def test_studio_creation_is_idempotent():
    first = await _ensure_studio()
    second = await _ensure_studio()

    assert first.id == second.id

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            count = len(
                (await db.scalars(select(Agent).where(Agent.is_builtin.is_(True)))).all()
            )
    assert count == 1


async def test_describe_message_carries_state_and_boundary():
    """关键字段必须写进 message。

    读工具的结果会经 `nodes._render_payload` 渲染，那个渲染器对列表只保留
    标题与标识（它是为站点列表写的）。线上第一次冒烟就栽在这里：
    模型只拿到一串名字，于是回答「返回里没有状态字段」并转去 clarify。
    """
    await _ensure_studio()
    await _create_agent("ops", allowed_tools=["get_platform_time"], can_write=False)

    result = await studio_tools.studio_describe(_ctx(), {"resource": "agent"})

    assert result.success is True
    message = result.message or ""
    assert "ops" in message
    assert "active" in message
    assert "只读" in message
    assert "工具：1 个" in message
    assert "studio" in message  # 内置助手也在清单里


async def test_describe_detail_message_includes_duty():
    await _ensure_studio()
    await _create_agent("cs", description="回答工单进度", status=AgentStatus.ACTIVE)

    result = await studio_tools.studio_describe(_ctx(), {"resource": "agent", "ref": "cs"})

    assert result.success is True
    message = result.message or ""
    assert "cs" in message
    assert "只读" in message
    assert "回答工单进度" in message


async def test_platform_admin_sees_write_tools_without_explicit_scope():
    """路由层按 `is_platform_admin or agent:write` 放行，工具目录必须同口径。

    不加这一位就会出现：同一个人在控制台点按钮能改配置，让助手在对话里改却
    只得到一个「我没有权限」的自然语言回答——没有报错、没有拒绝记录，
    看起来像模型不够聪明，实际是两处判断口径不同。
    """
    from app.tools.dependencies import get_tool_gateway

    await _ensure_studio()
    gateway = get_tool_gateway()

    # 只有 tools:read，没有 agent:write，但他是平台管理员。
    admin = _ctx(scopes=("tools:read",), is_platform_admin=True)
    admin_tools = {view.tool_name for view in gateway.list_available_for_context(context=admin)}
    assert {"studio_describe", "studio_agent_save", "studio_agent_delete"} <= admin_tools

    # 同样没有 agent:write 的普通成员：只看得见读工具。
    plain = _ctx(scopes=("tools:read",), is_platform_admin=False)
    plain_tools = {view.tool_name for view in gateway.list_available_for_context(context=plain)}
    assert "studio_describe" in plain_tools
    assert "studio_agent_save" not in plain_tools


async def test_two_builtin_agents_are_rejected_by_database():
    """唯一性交给部分唯一索引，不靠应用层自觉：并发创建时它才兜得住。

    这一条同时验证了 `is_builtin` 的索引真的建出来了——只用 `ensure_studio`
    的幂等用例测不出索引缺失，串行调用本来就只会建一次。
    """
    await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            db.add(
                Agent(
                    tenant_id=TENANT,
                    name="studio-two",
                    display_name="第二个构建助手",
                    description="",
                    instruction="",
                    allowed_tools=[],
                    can_write=False,
                    status=AgentStatus.ACTIVE,
                    is_default=False,
                    is_builtin=True,
                )
            )
            with pytest.raises(IntegrityError):
                await db.flush()


async def test_reserved_names_cannot_be_taken_by_users():
    await _ensure_studio()

    with pytest.raises(agent_service.AgentValidationError):
        await _create_agent(agent_service.STUDIO_AGENT_NAME)

    with pytest.raises(agent_service.AgentValidationError):
        await _create_agent("studio_assistant")


async def test_studio_boundary_cannot_be_edited():
    await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(db, ref=agent_service.STUDIO_AGENT_NAME)
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.update_agent(
                    db, agent=row, allowed_tools=["get_platform_time"]
                )
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.update_agent(db, agent=row, can_write=False)
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.update_agent(db, agent=row, allowed_skills=[])

            # 职责说明允许改：那是语气偏好，不改变能力。
            await agent_service.update_agent(db, agent=row, description="新的说明")
            assert row.description == "新的说明"


async def test_studio_cannot_be_disabled_or_deleted():
    await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.get_agent(db, ref=agent_service.STUDIO_AGENT_NAME)
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.set_status(db, agent=row, status=AgentStatus.DISABLED)
            with pytest.raises(agent_service.AgentValidationError):
                await agent_service.delete_agent(db, agent=row)


# ---------------- 读 ----------------


async def test_describe_lists_agents_including_studio():
    await _ensure_studio()
    await _create_agent("ops")

    result = await studio_tools.studio_describe(_ctx(), {"resource": "agent"})

    assert result.success is True
    names = {item["name"] for item in result.result["items"]}
    assert {"ops", agent_service.STUDIO_AGENT_NAME} <= names


async def test_describe_unknown_agent_fails_instead_of_inventing_one():
    await _ensure_studio()

    result = await studio_tools.studio_describe(_ctx(), {"resource": "agent", "ref": "nope"})

    assert result.success is False
    assert "没有找到" in (result.message or "")


# ---------------- 写 ----------------


async def test_save_creates_draft_readonly_agent():
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx(),
        {
            "mode": "create",
            "name": "cs",
            "display_name": "客服",
            "description": "只回答工单进度",
        },
    )

    assert result.success is True
    row = await _get("cs")
    assert row.status == AgentStatus.DRAFT
    assert row.can_write is False

    # 不传 allowed_tools = 不收窄。如果这里变成 []，说明代码把「没传」
    # 和「传了空数组」混为一谈了，新建出来的智能体会一个工具都调不动。
    assert row.allowed_tools is None
    message = result.message or ""
    assert "只读" in message
    assert "工具：不限" in message


async def test_save_message_flags_empty_tool_list():
    """`allowed_tools=[]` 合法但几乎总是误用，必须让模型看见后果。

    线上冒烟：用户说「新建只读客服智能体」，模型把「只读」读成
    `allowed_tools=[]`，造出一个调不动任何工具的空壳。审批卡片上只有参数，
    人看不出来；返回消息点名「一个都调不动」，助手才会主动提一句。
    """
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx(),
        {"mode": "create", "name": "cs", "display_name": "客服", "allowed_tools": []},
    )

    assert result.success is True
    message = result.message or ""
    assert "工具：无" in message
    assert "一个都调不动" in message


async def test_save_create_rejects_existing_name():
    await _ensure_studio()
    await _create_agent("ops")

    result = await studio_tools.studio_agent_save(
        _ctx(), {"mode": "create", "name": "ops", "display_name": "重复"}
    )

    assert result.success is False
    # 不能静默转成 update：那样名字打错就会悄悄改到别人身上。
    assert "已被占用" in (result.message or "")


async def test_save_update_rejects_missing_agent():
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(_ctx(), {"mode": "update", "name": "ghost"})

    assert result.success is False
    assert "没有找到" in (result.message or "")


async def test_save_update_touches_only_sent_fields():
    await _ensure_studio()
    await _create_agent("ops", allowed_tools=["get_platform_time"], can_write=False)

    result = await studio_tools.studio_agent_save(
        _ctx(), {"mode": "update", "name": "ops", "display_name": "运行值班"}
    )

    assert result.success is True
    row = await _get("ops")
    assert row.display_name == "运行值班"
    # 没传的边界字段保持原值，不能因为「保存了一次」被清空。
    assert row.allowed_tools == ["get_platform_time"]


async def test_save_refuses_to_grant_studio_tools_to_other_agents():
    """这是最要紧的一条：否则可以造出一个「能改配置且有审批记录」的 Agent。"""
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx(),
        {
            "mode": "create",
            "name": "shadow",
            "display_name": "影子",
            "allowed_tools": ["studio_agent_save"],
        },
    )

    assert result.success is False
    assert "不能把平台管理工具授予" in (result.message or "")
    with pytest.raises(agent_service.AgentNotFound):
        await _get("shadow")


async def test_save_refuses_tools_the_requester_cannot_use():
    """运行时不会越权（人 ∩ 边界），但配出来一条都调不动，必须在保存时就拒。"""
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx(),
        {
            "mode": "create",
            "name": "ops",
            "display_name": "运行值班",
            # 需要 maintenance.ticket.create，而当前身份没有这个 scope。
            "allowed_tools": ["create_maintenance_ticket"],
        },
    )

    assert result.success is False
    assert "你自己也没有权限" in (result.message or "")


async def test_save_studio_itself_is_rejected():
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx(),
        {
            "mode": "update",
            "name": agent_service.STUDIO_AGENT_NAME,
            "allowed_tools": ["get_platform_time"],
        },
    )

    assert result.success is False
    assert "内置智能体" in (result.message or "")


async def test_set_status_then_delete_requires_matching_name():
    await _ensure_studio()
    await _create_agent("tmp")

    activated = await studio_tools.studio_agent_set_status(
        _ctx(), {"name": "tmp", "status": "active"}
    )
    assert activated.success is True

    disabled = await studio_tools.studio_agent_set_status(
        _ctx(), {"name": "tmp", "status": "disabled"}
    )
    assert disabled.success is True

    refused = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "tmp", "confirm_name": "tpm"}
    )
    assert refused.success is False
    # 名字不对时不能落库，也不能「顺手删掉」。
    assert (await _get("tmp")).status == AgentStatus.DISABLED

    deleted = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "tmp", "confirm_name": "tmp"}
    )
    assert deleted.success is True
    with pytest.raises(agent_service.AgentNotFound):
        await _get("tmp")


async def test_delete_refuses_agent_that_is_still_active():
    await _ensure_studio()
    await _create_agent("live", status=AgentStatus.ACTIVE)

    result = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "live", "confirm_name": "live"}
    )

    assert result.success is False
    assert "先停用" in (result.message or "")


# ---------------- 审批前预检 ----------------
#
# 预检不是重复校验，它决定「要不要让人看到确认卡」。
# 注定失败的操作也弹一张卡，人点了几次都一样失败，之后就会开始闭眼点。


async def test_preflight_blocks_builtin_before_approval_card():
    """内置智能体在生成审批单之前就被挡住。

    线上冒烟暴露过：助手居然为「删除 studio」生成了一张确认卡。
    卡片本身就在误导人——它暗示这件事可能成功。
    """
    await _ensure_studio()

    for handler, preflight in (
        (studio_tools._preflight_mutation, "改动"),
        (studio_tools._preflight_delete, "删除"),
    ):
        reason = await handler(
            _ctx(), {"name": agent_service.STUDIO_AGENT_NAME}
        )
        assert reason is not None, f"{preflight}应当拦下内置智能体"
        assert "内置" in reason


async def test_preflight_allows_normal_agent():
    await _ensure_studio()
    await _create_agent("ops", status=AgentStatus.DISABLED)

    assert await studio_tools._preflight_mutation(_ctx(), {"name": "ops"}) is None
    assert await studio_tools._preflight_delete(_ctx(), {"name": "ops"}) is None


async def test_preflight_delete_requires_disabled_first():
    await _ensure_studio()
    await _create_agent("live", status=AgentStatus.ACTIVE)

    reason = await studio_tools._preflight_delete(_ctx(), {"name": "live"})

    assert reason is not None
    assert "先把它停用" in reason


async def test_preflight_ignores_missing_name():
    """名字没给时不抢话：让 handler 去报「需要指出标识」这种具体错误。"""
    await _ensure_studio()

    assert await studio_tools._preflight_mutation(_ctx(), {}) is None
    assert await studio_tools._preflight_delete(_ctx(), {}) is None


# ---------------- 会话入口判定 ----------------


async def test_builtin_name_is_recognized_as_studio():
    """`is_studio_agent_name` 的前提：`ensure_studio` 只会用这两个名字之一。

    会话的 channel 是按名字判的（调用点手上只有边界快照，没有 ORM 行）。
    哪天给它换了名字却没同步别名表，会话就不再记成 studio——面板照常能对话，
    只是刷新后读不到历史，属于「不报错但坏了」的那一类。
    """
    row = await _ensure_studio()

    assert row.is_builtin is True
    assert agent_service.is_studio_agent_name(row.name)
    assert not agent_service.is_studio_agent_name(None)
    assert not agent_service.is_studio_agent_name("")
    assert not agent_service.is_studio_agent_name("assistant")


# ---------------- 写操作必须留下资源级审计 ----------------

# 网关会给每次工具调用记一条 tool_succeeded，但那条的 resource 是工具本身
# （tool/studio_agent_save），回答不了「改了谁」。构建助手的会话在对话台里
# 看不到，审计就是唯一能追溯这件事的地方，所以这里逐条盯住资源事件。


async def _audits_of(resource_id: str) -> list:
    from app.models.audit import AuditLog

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog)
                .where(AuditLog.resource_id == resource_id)
                .order_by(AuditLog.created_at)
            )
            return list(rows.all())


def _ctx_with_approval(**kwargs):
    return _ctx(
        agent_name=agent_service.STUDIO_AGENT_NAME,
        agent_revision=4,
        approval_id="ap-9",
        **kwargs,
    )


async def test_save_create_writes_agent_created_audit():
    await _ensure_studio()

    result = await studio_tools.studio_agent_save(
        _ctx_with_approval(),
        {"mode": "create", "name": "cs", "display_name": "客服"},
    )

    assert result.success is True
    row = await _get("cs")
    audits = await _audits_of(str(row.id))

    assert [a.event_type.value for a in audits] == ["agent_created"]
    created = audits[0]
    assert created.resource_type == "agent"
    assert created.actor_id == "actor-1"
    assert created.thread_id == "thread-1"
    assert created.run_id == "run-1"
    assert created.metadata_json["via"] == "studio_assistant"
    assert created.metadata_json["tool_name"] == "studio_agent_save"
    # 审批号要与网关那条 tool_succeeded 对得上，否则「人确认过」这件事断了链。
    assert created.metadata_json["approval_id"] == "ap-9"
    assert created.metadata_json["agent_name"] == agent_service.STUDIO_AGENT_NAME
    assert created.metadata_json["agent_revision"] == 4
    assert created.metadata_json["status"] == "draft"
    assert created.metadata_json["can_write"] is False
    assert "构建助手" in (created.message or "")


async def test_save_update_records_boundary_diff():
    await _ensure_studio()
    await _create_agent("ops", can_write=False)

    row = await _get("ops")
    result = await studio_tools.studio_agent_save(
        _ctx_with_approval(),
        {"mode": "update", "name": "ops", "can_write": True},
    )

    assert result.success is True
    audits = await _audits_of(str(row.id))
    assert [a.event_type.value for a in audits] == ["agent_updated"]

    updated = audits[0]
    # 审计要能单独回答「放开过什么」，所以 before/after 都得有；
    # 只记 after 的话，事后看到 can_write=true 也判断不了是本来就开着还是刚放开。
    assert updated.metadata_json["before"]["can_write"] is False
    assert updated.metadata_json["after"]["can_write"] is True
    assert updated.metadata_json["before"]["revision"] < updated.metadata_json["after"]["revision"]


async def test_save_update_without_change_writes_no_audit():
    """反复保存同一份配置不该记「改过」——审计里的变更次数要可信。"""
    await _ensure_studio()
    await _create_agent("ops")

    row = await _get("ops")
    result = await studio_tools.studio_agent_save(
        _ctx(), {"mode": "update", "name": "ops", "display_name": "ops"}
    )

    assert result.success is True
    assert await _audits_of(str(row.id)) == []


async def test_status_and_delete_write_their_own_audits():
    await _ensure_studio()
    await _create_agent("ops")

    row = await _get("ops")
    await studio_tools.studio_agent_set_status(_ctx(), {"name": "ops", "status": "disabled"})
    deleted = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "ops", "confirm_name": "ops"}
    )

    assert deleted.success is True
    audits = await _audits_of(str(row.id))
    assert [a.event_type.value for a in audits] == ["agent_status_changed", "agent_deleted"]

    status_audit, delete_audit = audits
    assert status_audit.metadata_json["from"] == "draft"
    assert status_audit.metadata_json["to"] == "disabled"
    # 行已经删了，resource_id 仍然是它生前的 id：这样才能反查到「删的是哪一行」，
    # 而不是只剩一个名字。
    assert delete_audit.resource_id == str(row.id)
    assert delete_audit.metadata_json["name"] == "ops"


async def test_rejected_delete_writes_no_audit():
    """名字核不上就拒绝，不能留下「已删除」的记录——那会让审计说谎。"""
    await _ensure_studio()
    await _create_agent("ops")

    row = await _get("ops")
    result = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "ops", "confirm_name": "opss"}
    )

    assert result.success is False
    assert await _audits_of(str(row.id)) == []


# ---------------- 边界恢复 ----------------


async def test_restore_brings_back_previous_boundary():
    """回到某一版：只覆盖边界字段，且恢复本身会生成一个新的版本号。

    快照来自审计里的 before/after——平台没有「边界版本表」。这条用例同时
    盯住「创建事件必须记 revision」这个前提：少了它，v1 就找不回来。
    """
    await _ensure_studio()
    # 建也要走工具：只有工具会留下 `agent_created` 审计，而 v1 的快照只有那份。
    created = await studio_tools.studio_agent_save(
        _ctx(), {"mode": "create", "name": "ops", "can_write": False}
    )
    assert created.success is True, created.message
    await studio_tools.studio_agent_save(
        _ctx(), {"mode": "update", "name": "ops", "can_write": True}
    )

    row = await _get("ops")
    assert row.can_write is True
    assert row.revision == 2

    result = await studio_tools.studio_agent_restore(_ctx(), {"name": "ops", "revision": 1})

    assert result.success is True, result.message
    restored = await _get("ops")
    assert restored.can_write is False
    # 「回到第 1 版」是一次新的修改，不是把版本号倒回去——倒回去会让
    # 在途审批单的快照对上一个内容不同的版本。
    assert restored.revision == 3

    audits = await _audits_of(str(row.id))
    assert audits[-1].event_type.value == "agent_updated"
    assert audits[-1].metadata_json["restored_from"] == 1


async def test_restore_preflight_rejects_current_revision():
    await _ensure_studio()
    await _create_agent("ops")

    reason = await studio_tools._preflight_restore(_ctx(), {"name": "ops", "revision": 1})

    assert reason is not None
    assert "不需要恢复" in reason


async def test_restore_unknown_revision_lists_available():
    """审计有保留期，恢复不到就明说，不拿近似版本凑数。"""
    await _ensure_studio()
    await studio_tools.studio_agent_save(_ctx(), {"mode": "create", "name": "ops"})

    result = await studio_tools.studio_agent_restore(_ctx(), {"name": "ops", "revision": 7})

    assert result.success is False
    assert "v1" in (result.message or "")


async def test_restore_cannot_touch_builtin():
    await _ensure_studio()

    reason = await studio_tools._preflight_restore(
        _ctx(), {"name": agent_service.STUDIO_AGENT_NAME, "revision": 1}
    )

    assert reason is not None
    assert "内置" in reason


# ---------------- 助手自己的能力清单跟着代码走 ----------------


async def test_sync_studio_definition_restores_missing_tools_without_bumping_revision():
    """新加一个 studio_* 工具后，老租户的助手必须能拿到。

    不 bump revision 是刻意的：revision 记的是「人改过几次边界」，
    而工具清单是平台升级带过来的；bump 会让在途审批单的快照对不上。
    """
    studio = await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Agent).where(Agent.name == studio.name))
            before_revision = row.revision
            row.allowed_tools = ["studio_describe"]
            await db.commit()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            changed = await agent_service.sync_studio_definition(db, tenant_id=TENANT)
            row = await db.scalar(select(Agent).where(Agent.name == studio.name))

    assert changed is True
    assert "studio_agent_save" in (row.allowed_tools or [])
    assert row.revision == before_revision


def test_studio_extra_tools_are_read_only():
    """额外放行的能力只能是只读的。

    这条清单是「助手能自己决定调用」的工具。写进去一个写操作，
    就等于给内置助手开了一条不经过审批的写入口——而它恰恰是最容易被
    一段诱导性输入说服的那个角色。
    """
    from app.tools.dependencies import get_tool_gateway
    from app.tools.gateway import is_write_risk

    gateway = get_tool_gateway()
    for name in agent_service.STUDIO_EXTRA_TOOLS:
        # 业务读数来自连接器接入项，这个部署里可能一条都没接——没注册就无从
        # 谈风险级，跳过。真正挡住「手滑写成写工具」的是下面那条按名字判的用例。
        if not gateway.has_tool(name):
            continue
        tool = gateway.get_tool(name)
        assert not is_write_risk(tool.risk_level), name


def test_studio_extra_business_tools_are_reads_by_name():
    """额外放行的业务工具，名字里必须只有读动词。

    风险级要等工具注册进网关才查得到，而「别把写工具塞进这份名单」这件事
    得在代码评审阶段就挡住。名单是手写的，`fc_executestationcontrol` 这种
    名字里没有 get/list/query，这条断言当场拦下。

    只看动词前缀不看完整名单：接入项增删工具是常事，名单跟着改是正常的，
    但把一个 `execute`/`set`/`cancel` 写进「只读」清单永远不该发生。
    """
    assert agent_service.STUDIO_BUSINESS_READ_TOOLS, "名单不该为空，否则这条用例是摆设"

    for name in agent_service.STUDIO_BUSINESS_READ_TOOLS:
        verb = name.split("_", 1)[1]
        assert verb.startswith(("get", "list", "query")), name


async def test_studio_agent_can_read_attachments():
    """「+」传上来的文件必须有人能读。

    附件进提示词时只有一个句柄，识别靠 `ocr_document`。不把它放进助手的工具清单，
    面板上的「+」就是「上传一张谁也看不见的图」——模型会如实回答看不到内容，
    看起来像功能坏了，其实是边界把它挡在外面。
    """
    studio = await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Agent).where(Agent.name == studio.name))

    assert "ocr_document" in (row.allowed_tools or [])
    # 额外能力不能把 studio_* 挤掉：两者是并集，不是替换。
    assert "studio_agent_save" in (row.allowed_tools or [])


async def test_sync_studio_definition_is_noop_when_already_current():
    await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            assert await agent_service.sync_studio_definition(db, tenant_id=TENANT) is False


async def test_sync_studio_definition_refreshes_stale_instruction():
    """提示词也是代码定义的一部分，老租户的助手同样会停在旧版本上。

    停在旧版本的后果很安静：界面上「补充指令」看着一切正常，只是它还在说
    「只支持这几个工具」——助手于是拒绝做它其实已经能做的事。
    """
    studio = await _ensure_studio()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Agent).where(Agent.name == studio.name))
            row.instruction = "旧提示词：只支持 4 个工具。"
            await db.commit()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            changed = await agent_service.sync_studio_definition(db, tenant_id=TENANT)
            row = await db.scalar(select(Agent).where(Agent.name == studio.name))

    assert changed is True
    assert "密钥不经过对话" in (row.instruction or "")


# ---------------- 工具清单的呈现 ----------------
#
# 读工具这条路径不经过模型：`studio_describe(resource="tool")` 的 message 被
# `nodes._summarize_result` 直接当答复给用户。曾经每个工具独占一行（名称 +
# 显示名 + 风险级 + 来源），77 个工具就是七十多行文本，于是用户问「查一下 0 库存
# 物料」收到的是整整一屏工具目录。压缩成「分组一行」之后，同样的问题至少先是
# 一句关于工具数量与来源的判断，而不是一份抄给他的清单。


def _tool(name: str, risk: str = "read", source: str = "builtin") -> dict:
    return {
        "name": name,
        "display_name": name,
        "risk_level": risk,
        "requires_approval": risk != "read",
        "source": source,
    }


def test_tool_inventory_groups_by_source_instead_of_one_line_each():
    items = [_tool(f"read_{i}") for i in range(5)] + [_tool("write_x", "write")]

    text = studio_tools._tool_inventory(items)

    assert text.startswith("当前可调用 6 个工具：builtin 6 个（可写 1）。")
    assert text.count("\n") == 2, "一个来源占一行，再加一行下一步提示，不能退回逐条成行"
    assert "write_x〔写〕" in text, "写工具必须留在文本里：能不能动数据是用户最关心的"
    assert "read_0" in text


def test_tool_inventory_puts_write_tools_first_and_survives_truncation():
    """写工具永远排在组首：组内超过上限时被截掉的必须是只读工具。"""
    items = [_tool(f"a_read_{i:02d}") for i in range(30)] + [_tool("z_write", "write")]

    text = studio_tools._tool_inventory(items)

    names_line = text.splitlines()[1]
    assert names_line.split("：", 1)[1].startswith("z_write〔写〕"), "写工具排在组首，截断碰不到它"
    assert "等 31 个" in names_line, "截断了多少要写出来，否则模型以为工具就这么几个"


def test_tool_inventory_keeps_counts_when_every_group_is_large():
    items = [_tool(f"fc_{i:02d}", source="mcp") for i in range(30)] + [
        _tool(f"http_{i:02d}", "write", source="http") for i in range(3)
    ]

    text = studio_tools._tool_inventory(items)

    head = text.splitlines()[0]
    assert "mcp 30 个（可写 0）" in head
    assert "http 3 个（可写 3）" in head


def test_tool_inventory_tells_the_model_what_to_do_next():
    """清单末尾必须给下一步动作。

    线上反复出现「用户让删一批工具 → 模型调了本工具 → 把清单当答复交差」。
    模型对「工具刚告诉它的下一步」比系统提示词更服从，所以动作写在这里。
    """
    text = studio_tools._tool_inventory([_tool("prv_x")])

    tail = text.splitlines()[-1]
    assert tail.startswith("以上是清单，不是操作结果")
    assert "studio_tool_delete" in tail
    assert "不要只把清单交出去" in tail


def test_tool_inventory_groups_mcp_by_prefix_and_marks_code_registered():
    """用户说「某一批接口」时，清单里必须真有那一批的名字。

    这是 prv_ 时代的真实事故：按来源分组后 prv_ 排在 fc_ 后面，被每组上限整段
    截掉，模型手上没有名字，只能把 fc_ 的清单交出去。prv_ 那批已下线（改由
    FioCloud 连接器提供 fc_*），用例改用虚构名字，锁的是「按前缀分组、写工具
    不被截断」这个机制。
    另外代码注册的工具删不掉；不标出来，模型会去逐个撞拒绝。
    """
    items = [_tool(f"fc_tool_{index:02d}", source="mcp") for index in range(20)] + [
        _tool("prv_a", source="mcp"),
        _tool("prv_b", source="mcp"),
    ]

    text = studio_tools._tool_inventory(items, code_local={"prv_a", "prv_b"})

    lines = text.splitlines()
    prv_line = next(line for line in lines if "mcp/prv_" in line)
    assert "prv_a" in prv_line and "prv_b" in prv_line, "前缀是用户嘴里的名字，不能被截掉"
    assert "代码注册，删不掉" in prv_line
    assert any("mcp/fc_" in line for line in lines), "同一来源的另一个前缀也要在"


def test_tool_inventory_says_which_tools_can_still_be_removed():
    """同前缀下混着两类时，要点名可删的那几个。

    当时线上 prv_ 共 22 个：21 个是代码注册（只能改代码），只有
    prv_execute_station_control 是连接器接入项、能取消接入。用例沿用这组虚构
    名字——它测的是「同前缀下要点名可删的那几个」，不是那 22 个是否还在。
    只说「有删不掉的」，模型还是不知道该动哪一个，于是又反问用户。
    """
    items = [_tool("prv_a", source="mcp"), _tool("prv_b", source="mcp")]

    text = studio_tools._tool_inventory(items, code_local={"prv_a"})

    body = "\n".join(text.splitlines()[:-1])  # 最后一行是通用提示，本身含「代码注册」四个字
    assert "1 个代码注册、删不掉" in body
    assert "可删：prv_b" in body


async def test_empty_workflow_list_reads_like_something_a_person_says(monkeypatch):
    """0 张流程时那句正文，曾经原样出现在用户屏幕上。

    模型重复调同一个工具（同一参数）时，`nodes.plan_request` 的收敛分支直接拿
    工具的 message 当答案交出去。于是用户读到的是「新建用 studio_workflow_save
    mode=create，但要先 studio_describe resource=agent……」——给他看的正文里
    出现了他的工作里不存在的东西。

    读工具的 message 就是正文，所以这条分支只能写「还没有」与「你可以怎么说」。
    """

    async def _no_workflows(_tenant_id):
        return []

    async def _no_runs(**_kwargs):
        return []

    # 工具里是函数内 import，补丁要打在服务模块上。
    from app.services import workflow_service

    monkeypatch.setattr(workflow_service, "list_workflows", _no_workflows)
    monkeypatch.setattr(workflow_service, "list_runs", _no_runs)

    result = await studio_tools.studio_describe(_ctx(), {"resource": "workflow"})

    assert "还没有任何编排流程" in result.message
    assert "studio_" not in result.message
    assert "mode=" not in result.message
