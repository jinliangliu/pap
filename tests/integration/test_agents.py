# tests/integration/test_agents.py
"""智能体的接口层闭环：权限、生命周期，以及边界真的能拦住工具调用。

这里最该守住的一条是最后两个用例：**配置生效**。
一个只能展示、不能约束行为的「智能体管理页」等于没有——
所以除了 CRUD，还要验证「Agent 不允许的工具，发起调用会被拒绝」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.rbac import Role, RolePermission
from app.tenancy.context import tenant_scope

TENANT = "default"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


async def _grant_agent_write(session):
    """给 tester 角色加 `agent:write`。身份每次请求都重新解析，不需要清缓存。"""
    role = await session.scalar(select(Role).where(Role.name == "tester"))
    assert role is not None

    exists = await session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission == "agent:write",
        )
    )
    if exists is None:
        session.add(RolePermission(role_id=role.id, permission="agent:write"))
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


# ---------------- 读与权限 ----------------


async def test_member_can_list_agents_and_sees_default(client, seeded_identity):
    response = await client.get("/api/v1/agents")
    assert response.status_code == 200

    body = response.json()
    names = [item["name"] for item in body["items"]]

    # 默认 Agent 惰性建出：列表不能是空的，否则对话台的选择器无项可选。
    assert "assistant" in names
    assert body["can_manage"] is False

    default = next(item for item in body["items"] if item["is_default"])
    assert default["usable"] is True
    assert default["allowed_tools"] is None
    assert default["can_write"] is True


async def test_member_cannot_create_agent(client, seeded_identity):
    response = await client.post("/api/v1/agents", json={"name": "ops", "display_name": "运行值班"})
    assert response.status_code == 403


async def test_other_tenant_cannot_read_agent(client, other_client, seeded_identity):
    await _seed_agent(name="ops")

    # 本租户读得到
    own = await client.get("/api/v1/agents/ops")
    assert own.status_code == 200

    # 另一个租户拿到同一个标识 → 404（不是 403，避免成为存在性探针）
    other = await other_client.get("/api/v1/agents/ops")
    assert other.status_code == 404


# ---------------- 写与生命周期 ----------------


async def test_admin_can_create_update_and_activate(client, session, seeded_identity):
    await _grant_agent_write(session)

    created = await client.post(
        "/api/v1/agents",
        json={
            "name": "ops",
            "display_name": "运行值班",
            "description": "只回答运行规程问题。",
            "allowed_tools": ["get_station_snapshot"],
            "can_write": False,
        },
    )
    assert created.status_code == 201

    body = created.json()
    assert body["status"] == "draft"
    assert body["usable"] is False
    assert body["revision"] == 1
    assert body["allowed_tools"] == ["get_station_snapshot"]

    # 启用
    activated = await client.post("/api/v1/agents/ops/status", json={"status": "active"})
    assert activated.status_code == 200
    assert activated.json()["usable"] is True

    # 改边界 → revision 递增
    updated = await client.patch(
        "/api/v1/agents/ops", json={"allowed_tools": ["get_platform_time"]}
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["allowed_tools"] == ["get_platform_time"]


async def test_patch_without_field_keeps_boundary(client, session, seeded_identity):
    """只改职责文案不该把边界清空——这是最容易写错的一处。"""
    await _grant_agent_write(session)
    await client.post(
        "/api/v1/agents",
        json={"name": "ops", "allowed_tools": ["get_station_snapshot"]},
    )

    updated = await client.patch("/api/v1/agents/ops", json={"description": "负责规程问答"})
    assert updated.status_code == 200
    assert updated.json()["allowed_tools"] == ["get_station_snapshot"]


async def test_patch_with_explicit_null_clears_boundary(client, session, seeded_identity):
    """显式传 null = 取消收窄。与「没传」必须是两种结果。"""
    await _grant_agent_write(session)
    await client.post(
        "/api/v1/agents",
        json={"name": "ops", "allowed_tools": ["get_station_snapshot"]},
    )

    updated = await client.patch("/api/v1/agents/ops", json={"allowed_tools": None})
    assert updated.status_code == 200
    assert updated.json()["allowed_tools"] is None


async def test_patch_budget_keeps_zero_and_null_apart(client, session, seeded_identity):
    """0 是「不许转交」这个开关，null 是「回到平台默认」。

    两者都写在同一列上，混起来就再也撤不回一次收窄——界面上删掉数字
    应当回到默认，而填 0 应当继续阻止转交。
    """
    await _grant_agent_write(session)
    created = await client.post(
        "/api/v1/agents",
        json={"name": "mute", "max_delegation_depth": 0, "delegation_budget": 2},
    )
    assert created.status_code == 201
    assert created.json()["max_delegation_depth"] == 0
    assert created.json()["delegation_budget"] == 2

    # 没传 = 保持原值。
    kept = await client.patch("/api/v1/agents/mute", json={"description": "不转交"})
    assert kept.json()["max_delegation_depth"] == 0

    # 显式 null = 回到平台默认。
    cleared = await client.patch("/api/v1/agents/mute", json={"max_delegation_depth": None})
    assert cleared.status_code == 200
    assert cleared.json()["max_delegation_depth"] is None
    assert cleared.json()["delegation_budget"] == 2


async def test_agent_budget_out_of_range_is_400(client, session, seeded_identity):
    await _grant_agent_write(session)
    response = await client.post(
        "/api/v1/agents",
        json={"name": "deep", "max_delegation_depth": 9},
    )
    # 422 来自 schema 的边界，400 来自服务层；两道都算拦住了，但不能是 201。
    assert response.status_code in {400, 422}


async def test_default_agent_cannot_be_disabled(client, session, seeded_identity):
    await _grant_agent_write(session)
    await client.get("/api/v1/agents")  # 触发惰性创建

    response = await client.post("/api/v1/agents/assistant/status", json={"status": "disabled"})
    assert response.status_code == 400
    assert "默认" in response.json()["detail"]


async def test_delete_requires_disabled(client, session, seeded_identity):
    await _grant_agent_write(session)
    await client.post("/api/v1/agents", json={"name": "ops", "status": "active"})

    refused = await client.delete("/api/v1/agents/ops")
    assert refused.status_code == 400

    await client.post("/api/v1/agents/ops/status", json={"status": "disabled"})
    deleted = await client.delete("/api/v1/agents/ops")
    assert deleted.status_code == 204

    assert (await client.get("/api/v1/agents/ops")).status_code == 404


# ---------------- 对话接入 ----------------


async def test_chat_without_agent_uses_default(client, planner, seeded_identity):
    planner([ActionPlan(action="chat", response="好的。")])

    response = await client.post(
        "/api/v1/chat", json={"thread_id": "thread-agent-default", "message": "你好"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_chat_with_unknown_agent_is_404(client, seeded_identity):
    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-agent-404", "message": "你好", "agent": "ghost"},
    )
    assert response.status_code == 404


async def test_chat_with_disabled_agent_is_409(client, seeded_identity):
    await _seed_agent(name="ops", status=AgentStatus.DISABLED)

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-agent-409", "message": "你好", "agent": "ops"},
    )

    # 不静默回落到默认：「我选了它，它却换了一个人回答」比报错更难排查。
    assert response.status_code == 409
    assert "已停用" in response.json()["detail"]


async def test_agent_boundary_actually_blocks_tool_call(client, planner, seeded_identity, session):
    """核心用例：Agent 不允许的工具，模型发起调用也必须被拒。

    除了「拒了」，还要验证**拒得有账**：审计里得留下一条 `tool_denied`，
    并且写明是边界拦的。不然运营只能看到「有一堆拒绝」，不知道该改哪。
    """
    await _seed_agent(name="readonly", allowed_tools=[])

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_platform_time",
                arguments={},
                reason="查询服务器时间。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-agent-deny", "message": "现在几点？", "agent": "readonly"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "completed" or "未被授予" in body["answer"]
    assert "未被授予" in body["answer"]

    denied = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == AuditEventType.TOOL_DENIED,
                AuditLog.resource_id == "get_platform_time",
            )
        )
    ).scalars().all()
    assert denied, "边界拒绝没有落审计"
    metadata = denied[-1].metadata_json
    assert metadata["denial_reason"] == "agent"
    assert metadata["agent_name"] == "readonly"


async def test_agent_boundary_allows_listed_tool(client, planner, seeded_identity):
    await _seed_agent(name="allowed", allowed_tools=["get_platform_time"])

    planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_platform_time",
                arguments={},
                reason="查询服务器时间。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-agent-allow", "message": "现在几点？", "agent": "allowed"},
    )

    body = response.json()
    assert body["status"] == "completed"
    assert body["metadata"]["tool_name"] == "get_platform_time"


# ---------------- 内置构建助手 ----------------


async def test_list_lazily_creates_builtin_studio(client, seeded_identity):
    """第一次打开智能体页就该能问助手，不需要额外的初始化命令。"""
    response = await client.get("/api/v1/agents")
    assert response.status_code == 200

    builtin = [item for item in response.json()["items"] if item["is_builtin"]]
    assert len(builtin) == 1

    studio = builtin[0]
    assert studio["status"] == "active"
    assert studio["can_write"] is True
    assert studio["allowed_tools"]
    # 只放行管理工具，外加清单里显式声明的只读能力（附件识别要 ocr_document）：
    # 多放一个写工具，就是给内置助手开一条不经过审批的写入口。
    assert all(
        name.startswith("studio_") or name == "ocr_document"
        for name in studio["allowed_tools"]
    )
    # 内置不等于默认：不传 agent 的请求仍落到 assistant。
    assert studio["is_default"] is False


async def test_builtin_boundary_cannot_be_patched(client, session, seeded_identity):
    await _grant_agent_write(session)
    await client.get("/api/v1/agents")

    response = await client.patch(
        "/api/v1/agents/studio", json={"allowed_tools": ["get_platform_time"]}
    )

    assert response.status_code == 400
    assert "内置" in response.json()["detail"]


async def test_builtin_cannot_be_disabled_or_deleted(client, session, seeded_identity):
    await _grant_agent_write(session)
    await client.get("/api/v1/agents")

    disabled = await client.post("/api/v1/agents/studio/status", json={"status": "disabled"})
    assert disabled.status_code == 400

    deleted = await client.delete("/api/v1/agents/studio")
    assert deleted.status_code == 400


async def test_reserved_studio_name_cannot_be_created(client, session, seeded_identity):
    await _grant_agent_write(session)
    await client.get("/api/v1/agents")

    response = await client.post(
        "/api/v1/agents", json={"name": "studio", "display_name": "影子"}
    )

    assert response.status_code == 400
    assert "保留" in response.json()["detail"]


async def test_studio_write_stops_at_approval(client, planner, seeded_identity, session):
    """助手改配置必须停在审批：这是「二次确认」唯一的强制点。

    同时验证**审批通过前没有副作用**——如果这里能查到 cs，
    说明「先执行再问」已经悄悄发生了。
    """
    await _grant_agent_write(session)
    await client.get("/api/v1/agents")

    planner(
        [
            ActionPlan(
                action="write_tool",
                tool_name="studio_agent_save",
                arguments={
                    "mode": "create",
                    "name": "cs",
                    "display_name": "客服",
                    "can_write": True,
                },
                reason="用户要求新建客服智能体。",
            )
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-studio-approval",
            "message": "建一个客服 Agent",
            "agent": "studio",
        },
    )

    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["approval"]["preview"]["tool_name"] == "studio_agent_save"

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            count = len((await db.scalars(select(Agent).where(Agent.name == "cs"))).all())
    assert count == 0
