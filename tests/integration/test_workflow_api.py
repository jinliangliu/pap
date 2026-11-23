# tests/integration/test_workflow_api.py
"""编排的接口层闭环：真的配一张图、真的在对话里跑起来、真的留下可对账的记录。

这里守的是几条只有集成才看得见的规则：

- **图的错误在保存时就拒绝**，不是等到对话里才炸；
- **深度预检**在开跑之前就给出可操作的理由，而不是跑到第三个节点才死；
- **次数与深度分账**：人写死的 5 步不会被「模型每轮最多 3 次」卡死，
  但深度照旧逐跳累积；
- **审计带 `workflow_run_id` / `node_id`**，否则「这条边是谁发起」只能靠猜。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.rbac import Role, RolePermission
from app.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from app.tenancy.context import tenant_scope

TENANT = "default"


@pytest.fixture(autouse=True)
async def _agent_write(session, seeded_identity):
    """临时给 tester 角色加 `agent:write`，结束时收回。

    不能永久加：`test_agent_delegate.py` 有一条断言依赖「tester 没有 agent:write」
    （构建助手工具的边界求交证据）。永久改角色会让那条测试在文件顺序变化时
    莫名其妙地变绿或变红。
    """
    role = await session.scalar(select(Role).where(Role.name == "tester"))
    assert role is not None

    existing = await session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission == "agent:write",
        )
    )

    if existing is None:
        session.add(RolePermission(role_id=role.id, permission="agent:write"))
        await session.commit()

    yield

    if existing is None:
        await session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission == "agent:write",
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean(session):
    await session.execute(delete(WorkflowRun))
    await session.execute(delete(Workflow))
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(WorkflowRun))
    await session.execute(delete(Workflow))
    await session.execute(delete(Agent))
    await session.commit()


async def _seed_agent(**kwargs) -> str:
    from app.services import agent_service

    name = kwargs.pop("name")
    display_name = kwargs.pop("display_name", name)

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name=name,
                display_name=display_name,
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                **kwargs,
            )
            await db.commit()
            return str(row.id)


def _work_plan(answer: str) -> list[ActionPlan]:
    """一个节点的两次规划：先查一次（留下依据），再作答。

    为什么必须有第一步：`invoke` 会拦「没有任何依据的结论」，
    一段凭空生成的答复过不了桥。
    """
    return [
        ActionPlan(action="read_tool", tool_name="get_platform_time", reason="对齐时间窗"),
        ActionPlan(action="chat", response=answer),
    ]


async def _chat(client, thread: str, message: str, agent: str = "cs"):
    """以 `cs` 的身份发一句话。

    必须点名 Agent：不指定时用的是平台默认 Agent，它没有 `max_delegation_depth`
    配置——深度预检会按平台默认的 2 判，测出来的数是错的。
    """
    return await client.post(
        "/api/v1/chat",
        json={"thread_id": thread, "message": message, "agent": agent},
    )


async def _save(client, definition: dict, *, name: str = "triage") -> dict:
    response = await client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "display_name": "告警排查",
            "description": "分类 → 排查 → 汇总",
            "definition": definition,
        },
    )
    return {"status": response.status_code, "body": response.json()}


TWO_STEP = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs", "task": "判断类别：{{input.text}}",
         "emit": {"field": "category"},
         "next": [{"when": None, "to": "diagnose"}]},
        {"id": "diagnose", "agent": "ops", "task": "按 {{nodes.classify}} 排查",
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}


async def test_save_rejects_broken_definition(client, seeded_identity):
    """没有兜底边 → 保存时就拒绝，并指出是哪个节点。"""
    broken = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agent": "cs", "task": "x", "emit": {"field": "k"},
             "next": [{"when": {"key": "k", "op": "eq", "value": "1"}, "to": "b"}]},
            {"id": "b", "kind": "compose", "task": "done", "next": []},
        ],
    }

    result = await _save(client, broken)
    assert result["status"] == 422
    assert result["body"]["detail"]["code"] == "WORKFLOW_NO_FALLBACK"
    # 前端靠 `node` 把出错的那张卡片标红。少了它只能显示一句 message，
    # 图一长就变成「自己去找哪一步」。
    assert result["body"]["detail"]["node"] == "a"


async def test_saved_workflow_is_listed_with_shape(client, seeded_identity):
    await _seed_agent(name="cs", allowed_skills=None)
    await _seed_agent(name="ops")

    saved = await _save(client, TWO_STEP)
    assert saved["status"] == 200
    assert saved["body"]["revision"] == 1
    assert saved["body"]["node_count"] == 3
    # 汇总节点不调 Agent，所以只算 2 跳。
    assert saved["body"]["depth_hops"] == 2

    listing = await client.get("/api/v1/workflows")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    # 控制台据此决定要不要显示「新建 / 编辑 / 删除」：读接口不带这个标记，
    # 前端就只能猜，猜错的表现是「按钮都在，点下去全是 403」。
    assert listing.json()["can_manage"] is True
    assert listing.json()["items"][0]["name"] == "triage"


async def test_chat_runs_the_flow_and_audits_every_node(
    client, planner, seeded_identity
):
    """一次两节点的 run：用户看到最后那句，审计里每个节点各一条。"""
    await _seed_agent(name="cs", allowed_skills=None)
    await _seed_agent(name="ops")

    await _save(client, TWO_STEP)

    planner(_work_plan("这是电控问题") + _work_plan("检查变频器散热"))

    response = await _chat(client, "wf-1", "/workflow triage 3 号泵跳停")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert "检查变频器散热" in body["answer"]
    assert body["metadata"]["workflow"] == "triage"

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            runs = (await db.scalars(select(WorkflowRun))).all()
            assert len(runs) == 1
            run = runs[0]
            assert run.status == WorkflowRunStatus.COMPLETED
            assert run.nodes_used == 3
            assert [item["node_id"] for item in run.nodes_json] == [
                "classify",
                "diagnose",
                "answer",
            ]

            events = (
                await db.scalars(
                    select(AuditLog).where(
                        AuditLog.event_type == AuditEventType.AGENT_DELEGATED
                    )
                )
            ).all()
            assert len(events) == 2
            metas = [event.metadata_json for event in events]
            assert {m["node_id"] for m in metas} == {"classify", "diagnose"}
            assert {m["workflow_run_id"] for m in metas} == {str(run.id)}
            # 第二条的上级是第一条的 Agent，不是根。
            by_node = {m["node_id"]: m for m in metas}
            assert by_node["diagnose"]["from_agent"] == "cs"


async def test_depth_is_prechecked_before_any_call(client, planner, seeded_identity):
    """发起方「不许转交」（0 跳）→ 开跑前就拒，并说清该改哪个数。

    注意判据是「还能不能转交」，不是「图里有几个节点」：编排的每一跳都以
    发起方为父，运行时深度恒为 1，所以 3 节点的图在 `max_depth=2` 下本来就该跑得起来。
    """
    await _seed_agent(name="cs", allowed_skills=None, max_delegation_depth=0)
    await _seed_agent(name="ops")

    three = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agent": "cs", "task": "一",
             "next": [{"when": None, "to": "b"}]},
            {"id": "b", "agent": "ops", "task": "二",
             "next": [{"when": None, "to": "c"}]},
            {"id": "c", "agent": "ops", "task": "三", "next": []},
        ],
    }
    await _save(client, three, name="deep")

    planner([])
    response = await _chat(client, "wf-depth", "/workflow deep 试试")
    body = response.json()

    assert body["status"] == "error", body.get("answer")
    assert body["error_category"] == "WORKFLOW_DEPTH_EXCEEDED", body.get("error_category")
    # 理由要能照着改：说清现在是几跳、该调到多少。
    assert "0 跳" in body["answer"] and "至少 1 跳" in body["answer"]

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            run = await db.scalar(select(WorkflowRun))
            assert run is not None
            assert run.status == WorkflowRunStatus.FAILED
            assert run.nodes_used == 0


async def test_five_nodes_finish_despite_a_tiny_per_turn_budget(
    client, planner, seeded_identity
):
    """次数与深度分账：图上 5 步，模型的「每轮最多 1 次转交」不该卡住它。"""
    await _seed_agent(
        name="cs",
        allowed_skills=None,
        delegation_budget=1,
        max_delegation_depth=5,
    )
    await _seed_agent(name="ops", max_delegation_depth=5)

    nodes = [
        {"id": f"s{i}", "agent": "ops", "task": f"第 {i} 步",
         "next": ([{"when": None, "to": f"s{i + 1}"}] if i < 4 else [])}
        for i in range(5)
    ]
    await _save(client, {"entry": "s0", "max_nodes": 6, "nodes": nodes}, name="five")

    plans: list[ActionPlan] = []
    for i in range(5):
        plans += _work_plan(f"第{i}步完成")
    planner(plans)

    response = await _chat(client, "wf-five", "/workflow five 巡检")
    body = response.json()

    assert body["status"] == "completed", body.get("answer")
    assert body["metadata"]["nodes_used"] == 5

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            run = await db.scalar(select(WorkflowRun))
            assert run.nodes_used == 5
            assert run.status == WorkflowRunStatus.COMPLETED


async def test_unknown_workflow_lists_what_exists(client, seeded_identity):
    await _seed_agent(name="cs", allowed_skills=None)
    # 图里两个步骤分别用 cs 与 ops：保存时逐个核对智能体是否存在（见
    # `workflow_service._ensure_agents_available`），少一个就存不进去。
    await _seed_agent(name="ops")
    await _save(client, TWO_STEP)

    response = await _chat(client, "wf-list", "/workflow")
    body = response.json()

    assert body["status"] == "completed"
    assert "triage" in body["answer"]

    missing = await _chat(client, "wf-list", "/workflow nope 一件事")
    assert missing.json()["status"] == "error"
    assert "nope" in missing.json()["answer"]


async def test_runs_endpoint_exposes_node_flow(client, planner, seeded_identity):
    await _seed_agent(name="cs", allowed_skills=None)
    await _seed_agent(name="ops")
    await _save(client, TWO_STEP)

    planner(_work_plan("电控") + _work_plan("查散热"))

    await _chat(client, "wf-runs", "/workflow triage 泵跳停")

    listing = await client.get("/api/v1/workflows/triage/runs")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert [n["node_id"] for n in items[0]["nodes"]] == ["classify", "diagnose", "answer"]
    assert items[0]["nodes"][0]["agent"] == "cs"

    detail = await client.get(f"/api/v1/workflow-runs/{items[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["output_text"]


async def test_flow_can_be_selected_instead_of_typed(client, planner, seeded_identity):
    """界面上直接选一条流程（`workflow` 字段），不敲 `/workflow`。

    两个入口必须落到同一件事上：同一条 run、同样逐跳判定、同样落 metadata。
    分开实现的话，最典型的分叉是「选了流程但没做深度预检」——跑起来才发现
    第三跳被拒，而用户只是点了个下拉。
    """
    await _seed_agent(name="cs", allowed_skills=None)
    await _seed_agent(name="ops")

    await _save(client, TWO_STEP)
    planner(_work_plan("这是电控问题") + _work_plan("检查变频器散热"))

    # 建智能体时会顺手建出租户的默认 Agent（`assistant`），所以基线要在发消息之前取：
    # 这条断言问的是「选了流程这一路有没有再多建一个」，不是「库里只有两个人」。
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            before = sorted(a.name for a in (await db.scalars(select(Agent))).all())

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "wf-picked", "message": "3 号泵跳停", "workflow": "triage"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    # 消息正文原样当输入，没有被改写成 `/workflow triage 3 号泵跳停`。
    assert "检查变频器散热" in body["answer"]
    assert body["metadata"]["workflow"] == "triage"
    assert body["metadata"]["nodes_used"] == 3

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            runs = (await db.scalars(select(WorkflowRun))).all()
            assert len(runs) == 1
            assert runs[0].status == WorkflowRunStatus.COMPLETED
            # 选了流程就不该顺手创建出租户的默认 Agent：这条路径根本不看「谁回答」。
            after = sorted(a.name for a in (await db.scalars(select(Agent))).all())
            assert after == before


async def test_agent_and_flow_together_is_rejected(client, seeded_identity):
    """同时选人和选流程 → 422，而不是静默丢掉其中一个。

    流程每一步用节点自带的智能体，`agent` 在这条路径上没有任何作用。悄悄忽略的话，
    用户看到的是「我选了客服，回答却来自别人」，而这在界面上无从查起。
    """
    await _seed_agent(name="cs", allowed_skills=None)
    await _save(client, TWO_STEP)

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "wf-both",
            "message": "3 号泵跳停",
            "agent": "cs",
            "workflow": "triage",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CHAT_TARGET_CONFLICT"
