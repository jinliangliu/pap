# tests/integration/test_workflow_pause.py
"""编排停在写操作上：等一个人，然后从停住的地方接着跑。

这是设计稿 §10 里唯一一条「跨两个请求」的验收：一次 run 的生命周期被审批切成两段，
中间还夹着一次人的点击。它守的是四件在实现里最容易走偏的事：

1. **暂停不新开一条 run**。续跑接着同一行写，否则运行页上会看到两次「跑到一半」；
2. **不重跑停住的节点**。重跑等于把同一个写操作再写一次——那正是审批要防的事；
3. **`nodes_used` 不清零**。从 0 重算会让「5 步上限」在每次审批后重新变满；
4. **`nodes_json` 不丢前半段**。续跑只写回它自己那几步的话，一条停过审批的 run
   在运行页上会看起来是从第二个节点凭空开始的。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.approval import ApprovalRequest
from app.models.audit import AuditEventType, AuditLog
from app.models.rbac import Role, RolePermission
from app.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from app.tenancy.context import tenant_scope

TENANT = "default"

WRITE_ARGS = {
    "station_id": "station-001",
    "title": "3 号泵压力异常",
    "description": "出口压力低于阈值，需要现场检查。",
}


@pytest.fixture(autouse=True)
async def _agent_write(session, seeded_identity):
    """临时给 tester 加 `agent:write`（保存流程要用），结束时收回。

    与 `test_workflow_api.py` 里那份同因：`test_agent_delegate.py` 有一条断言
    依赖「tester 没有 agent:write」。
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


async def _seed_pair() -> None:
    """客服（能分类、也能建单）+ 运行值班（只能建单）。

    客服必须也带 `create_maintenance_ticket`：子集校验要求下一跳的显式清单是
    上一跳的子集，否则第二个节点在调用前就被拒，根本停不到审批上。
    """
    await _seed_agent(
        name="cs",
        display_name="客服",
        allowed_tools=["get_platform_time", "create_maintenance_ticket"],
        can_write=True,
        max_delegation_depth=3,
    )
    await _seed_agent(
        name="ops",
        display_name="运行值班",
        allowed_tools=["create_maintenance_ticket"],
        can_write=True,
        max_delegation_depth=3,
    )


# 中间那个节点会停下来等审批；后面那个节点用来证明「续跑真的接着往下走了」。
PAUSING = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs", "task": "判断类别：{{input.text}}",
         "next": [{"when": None, "to": "execute"}]},
        {"id": "execute", "agent": "ops", "task": "为「{{nodes.classify}}」建维护工单",
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "流程结论：{{upstream}}", "next": []},
    ],
}


async def _save(client, definition: dict, *, name: str = "ticket") -> dict:
    response = await client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "display_name": "建单流程",
            "description": "分类 → 建单 → 汇总",
            "definition": definition,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _plans() -> list[ActionPlan]:
    """三个计划：分类节点的两步，加第二个节点的那次写操作。

    恢复之后**不再有一次规划**：停住的那个节点的结论由审批执行结果直接给回来，
    子图从审批那一跳收尾。多塞一个 `chat` 计划反而会掩盖这条路径是否真的走通了。
    """
    return [
        ActionPlan(action="read_tool", tool_name="get_platform_time", reason="对齐时间窗"),
        ActionPlan(action="chat", response="分类：电控类故障"),
        ActionPlan(
            action="write_tool",
            tool_name="create_maintenance_ticket",
            arguments=WRITE_ARGS,
            reason="用户要求建一张维护工单。",
        ),
    ]


async def _start(client, planner, thread: str = "wf-pause") -> dict:
    """跑到停住为止，返回这一次对话的响应体。"""
    planner(_plans())

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread, "message": "/workflow ticket 3 号泵跳停", "agent": "cs"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _run(run_id: str) -> WorkflowRun:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.get(WorkflowRun, uuid.UUID(run_id))
            assert row is not None
            db.expunge(row)
            return row


async def _approvals_of(run: WorkflowRun) -> list[ApprovalRequest]:
    """这条 run 派生出来的审批单。

    按子线程前缀筛，而不是数全表：同一次测试里别的用例也会留下单据，数全表
    得到的数字与这条 run 无关。
    """
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.thread_id.like(f"{run.thread_id}:d%")
                )
            )
            return list(rows)


async def _delegated_events(node_id: str, *, workflow_run_id: str) -> list[AuditLog]:
    """这条 run 里某个节点被调用了几次。

    必须按 `workflow_run_id` 收窄：审计表不清，别的用例也会跑一个叫 `classify`
    的节点，数全表得到的是「今天有多少个节点叫这个名字」。
    """
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog).where(
                    AuditLog.event_type == AuditEventType.AGENT_DELEGATED
                )
            )
            return [
                row
                for row in rows
                if (row.metadata_json or {}).get("node_id") == node_id
                and (row.metadata_json or {}).get("workflow_run_id") == workflow_run_id
            ]


async def test_pause_then_confirm_continues_from_the_same_node(
    client, planner, seeded_identity
):
    """整条闭环：跑两节点 → 停在写操作 → 人点确认 → 从停住的那个节点接着跑完。"""
    await _seed_pair()
    await _save(client, PAUSING)

    body = await _start(client, planner)

    # 用户在对话里看到的是一次「等人确认」，与他自己发起写操作时同一个形态。
    assert body["status"] == "awaiting_approval", body.get("answer")
    approval_id = body["approval"]["approval_id"]
    assert approval_id
    run_id = body["metadata"]["workflow_run_id"]

    run = await _run(run_id)
    assert run.status == WorkflowRunStatus.PAUSED
    assert run.approval_id == approval_id
    # 游标停在停住的那个节点上——续跑靠它，不靠「记住发生过什么」。
    assert run.cursor == "execute"
    # 停住的那个节点还没有产出，不算进已用次数。
    assert run.nodes_used == 1
    assert [item["node_id"] for item in run.nodes_json] == ["classify", "execute"]
    assert run.nodes_json[-1]["status"] == "paused"
    assert run.finished_at is None

    # 只有一张单子：暂停复用既有审批链路，不另开一张。
    assert len(await _approvals_of(run)) == 1

    confirm = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirm.status_code == 200
    result = confirm.json()
    assert result["success"] is True, result.get("message")

    # 续跑的结论要回到同一段回答里：用户点完就该看到「流程跑完了」。
    assert "流程继续执行完毕" in result["message"]
    # 汇总节点用的是「建单节点」的结论，而那个结论来自审批执行，不是模型转述。
    assert "流程结论：" in result["message"]
    assert "操作已执行" in result["message"]

    after = await _run(run_id)
    assert str(after.id) == str(run.id), "续跑另开了一条 run"
    assert after.status == WorkflowRunStatus.COMPLETED
    assert after.finished_at is not None
    # 1（分类）+ 1（建单，恢复后计入）+ 1（汇总），不是从 0 重来。
    assert after.nodes_used == 3
    assert [item["node_id"] for item in after.nodes_json] == [
        "classify",
        "execute",
        "answer",
    ]
    assert after.nodes_json[1]["status"] == "ok"
    assert "操作已执行" in (after.output_text or "")

    # 续跑没有额外开单。
    assert len(await _approvals_of(after)) == 1

    # 停住的那个节点只被调用过一次：再调一次就是再写一次外部系统。
    assert len(await _delegated_events("execute", workflow_run_id=run_id)) == 1
    assert len(await _delegated_events("classify", workflow_run_id=run_id)) == 1


async def test_cancel_stops_the_flow_where_it_paused(client, planner, seeded_identity):
    """取消 → 整条流程取消，后面的节点一步都不跑。

    「用户拒绝了写操作，流程替他继续做下一步决定」是最坏的形态：它会把一次
    明确的拒绝变成一串悄无声息的后果。
    """
    await _seed_pair()
    await _save(client, PAUSING)

    body = await _start(client, planner, thread="wf-pause-cancel")
    approval_id = body["approval"]["approval_id"]
    run_id = body["metadata"]["workflow_run_id"]

    cancel = await client.post(f"/api/v1/approvals/{approval_id}/cancel")
    assert cancel.status_code == 200

    after = await _run(run_id)
    assert after.status == WorkflowRunStatus.CANCELLED
    assert after.error_code == "approval_rejected"
    assert after.nodes_used == 1
    assert [item["node_id"] for item in after.nodes_json] == ["classify", "execute"]
    # 汇总节点没有跑：它连一条记录都不该有。
    assert await _delegated_events("answer", workflow_run_id=run_id) == []


async def test_resume_uses_the_frozen_graph_not_the_new_one(
    client, planner, seeded_identity
):
    """批准期间有人改了图 → 这条 run 按它开始时的图走完。

    拿新图跑旧游标，轻则节点找不到，重则跳过某个本该执行的节点。所以 run 行上
    存了一份图副本，续跑读的是它。
    """
    await _seed_pair()
    await _save(client, PAUSING)

    body = await _start(client, planner, thread="wf-pause-edit")
    approval_id = body["approval"]["approval_id"]
    run_id = body["metadata"]["workflow_run_id"]
    assert (await _run(run_id)).status == WorkflowRunStatus.PAUSED

    # 改图：汇总节点的措辞换了，revision 也随之 +1。
    edited = {
        "entry": "classify",
        "nodes": [
            {"id": "classify", "agent": "cs", "task": "判断类别：{{input.text}}",
             "next": [{"when": None, "to": "execute"}]},
            {"id": "execute", "agent": "ops", "task": "为「{{nodes.classify}}」建维护工单",
             "next": [{"when": None, "to": "answer"}]},
            {"id": "answer", "kind": "compose", "task": "【改过的模板】{{upstream}}", "next": []},
        ],
    }
    saved = await _save(client, edited, name="ticket")
    assert saved["revision"] == 2

    confirm = await client.post(f"/api/v1/approvals/{approval_id}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["success"] is True

    after = await _run(run_id)
    assert after.status == WorkflowRunStatus.COMPLETED
    # 跑的是启动时冻结的那版图。
    assert after.revision == 1
    assert "【改过的模板】" not in confirm.json()["message"]
    assert (after.output_text or "").startswith("流程结论：")
