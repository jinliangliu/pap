# tests/integration/test_workflow_audit.py
"""控制台改一张编排图，必须留下可对账的记录。

守的是这一条：**`workflows.py` 的四个写动作各写一条审计**。此前这个文件一处审计
都没有（agents / skills / connectors 都有），于是从控制台删掉一张图之后：

- 查不出谁、什么时候删的；
- `verify_workflow_deleted` 要的那条「按 id 找 `workflow_deleted`」正向证据
  永远补不出来，删除一律停在「核不出结论」。

所以这里的断言不止「有没有一条记录」，还包括**记录里必须有的字段**：
`resource_id` 是被删那一行的 id（不是名字——名字可以被复用），`metadata.operation`
是核验侧认的那个字符串。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.models.rbac import Role, RolePermission
from app.models.workflow import Workflow, WorkflowRun
from app.tenancy.context import tenant_scope

TENANT = "default"


@pytest.fixture(autouse=True)
async def _agent_write(session, seeded_identity):
    """临时给 tester 角色加 `agent:write`，结束时收回。

    与 `test_workflow_api.py` 同一套做法：不能永久加，`test_agent_delegate.py`
    有一条断言依赖「tester 没有 agent:write」。
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
    """每条用例都从「库里没有流程、审计里没有编排记录」开始。

    `audit_logs` 只追加、不随资源删除而消失，所以一张图被删掉之后那几条记录
    仍然在。不清掉它们，「这次有没有写审计」就只能靠数总数——前一条用例留下的
    一行会让断言静默变绿或变红。
    """
    await session.execute(delete(WorkflowRun))
    await session.execute(delete(Workflow))
    await session.execute(delete(Agent))
    await session.execute(delete(AuditLog).where(AuditLog.resource_type == "workflow"))
    await session.commit()
    yield
    await session.execute(delete(WorkflowRun))
    await session.execute(delete(Workflow))
    await session.execute(delete(Agent))
    await session.execute(delete(AuditLog).where(AuditLog.resource_type == "workflow"))
    await session.commit()


async def _seed_agent(session, name: str) -> str:
    from app.services import agent_service

    with tenant_scope(TENANT):
        row = await agent_service.create_agent(
            session,
            tenant_id=TENANT,
            name=name,
            display_name=name,
            status=AgentStatus.ACTIVE,
        )
    await session.commit()
    return str(row.id)


async def _audits(event_type: AuditEventType) -> list[AuditLog]:
    """另起会话读审计：写操作各自提交在别的会话里，用同一个会话读会看不见。"""
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == event_type)
                .order_by(AuditLog.created_at)
            )
            return list(rows)


async def _workflow_id(name: str) -> str | None:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Workflow).where(Workflow.name == name))
            return str(row.id) if row is not None else None


TWO_STEP = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs", "task": "判断类别：{{input.text}}",
         "emit": {"field": "category"},
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}

THREE_STEP = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs", "task": "判断类别：{{input.text}}",
         "emit": {"field": "category"},
         "next": [{"when": None, "to": "diagnose"}]},
        {"id": "diagnose", "agent": "cs", "task": "按 {{nodes.classify}} 排查",
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}


async def _save(client, definition: dict, *, name: str = "triage", display: str = "告警排查"):
    return await client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "display_name": display,
            "description": "分类 → 汇总",
            "definition": definition,
        },
    )


async def test_create_writes_audit_with_row_id(client, session, seeded_identity):
    await _seed_agent(session, "cs")

    response = await _save(client, TWO_STEP)
    assert response.status_code == 200
    workflow_id = await _workflow_id("triage")
    assert workflow_id is not None

    rows = await _audits(AuditEventType.WORKFLOW_CREATED)
    assert len(rows) == 1
    meta = rows[0].metadata_json
    assert rows[0].resource_type == "workflow"
    # 记 id 不记名字：名字删掉之后可以被新建的图复用，按名字回查会指到另一张图。
    assert rows[0].resource_id == workflow_id
    assert meta["operation"] == "workflow_created"
    assert meta["name"] == "triage"
    assert meta["revision"] == 1
    assert meta["graph_changed"] is True
    assert meta["node_count"] == 2
    # 审计里不放图本身：`definition` 是整份配置，复制进长期表没有对账价值。
    assert "definition" not in meta


async def test_update_and_rename_are_distinguishable(client, session, seeded_identity):
    await _seed_agent(session, "cs")

    assert (await _save(client, TWO_STEP)).status_code == 200
    # 换图：节点多一个，修订号 +1，`graph_changed` 为真。
    assert (await _save(client, THREE_STEP)).status_code == 200
    # 只换显示名：图原样传回，修订号不动，`graph_changed` 为假。
    assert (await _save(client, THREE_STEP, display="告警排查（改名）")).status_code == 200

    rows = await _audits(AuditEventType.WORKFLOW_UPDATED)
    assert len(rows) == 2
    graph_edit, rename_only = (rows[0].metadata_json, rows[1].metadata_json)

    assert graph_edit["revision"] == 2
    assert graph_edit["graph_changed"] is True
    assert graph_edit["node_count"] == 3

    # 这一条是「看着像重复记录」的那一类：版本号与上一条相同。
    # 区分它们的是 `graph_changed`，不是 revision。
    assert rename_only["revision"] == 2
    assert rename_only["graph_changed"] is False
    assert rename_only["display_name"] == "告警排查（改名）"


async def test_rejected_save_writes_no_audit(client, session, seeded_identity):
    await _seed_agent(session, "cs")

    broken = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agent": "cs", "task": "x", "emit": {"field": "k"},
             "next": [{"when": {"key": "k", "op": "eq", "value": "1"}, "to": "b"}]},
            {"id": "b", "kind": "compose", "task": "done", "next": []},
        ],
    }
    assert (await _save(client, broken)).status_code == 422

    # 没落库的改动不该在审计里留下「新建过」的痕迹。
    assert await _audits(AuditEventType.WORKFLOW_CREATED) == []


async def test_toggle_writes_status_change_once(client, session, seeded_identity):
    await _seed_agent(session, "cs")
    assert (await _save(client, TWO_STEP)).status_code == 200

    off = await client.patch("/api/v1/workflows/triage", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False

    # 再停一次：状态没变，不该多一条记录——否则「被停过几次」查出来是假的。
    again = await client.patch("/api/v1/workflows/triage", json={"enabled": False})
    assert again.status_code == 200

    rows = await _audits(AuditEventType.WORKFLOW_STATUS_CHANGED)
    assert len(rows) == 1
    meta = rows[0].metadata_json
    assert meta["from"] is True
    assert meta["to"] is False
    assert rows[0].resource_id == await _workflow_id("triage")


async def test_delete_writes_evidence_verifier_can_find(client, session, seeded_identity):
    """删除那条记录必须能被 `verify_workflow_deleted` 认出来。

    它按 `metadata.operation == "workflow_deleted"` + `resource_id` 找证据；
    两个字段少一个，控制台删掉的图就永远「核不出结论」。
    """
    await _seed_agent(session, "cs")
    assert (await _save(client, TWO_STEP)).status_code == 200
    workflow_id = await _workflow_id("triage")
    assert workflow_id is not None

    response = await client.delete("/api/v1/workflows/triage")
    assert response.status_code == 204
    assert await _workflow_id("triage") is None

    rows = await _audits(AuditEventType.WORKFLOW_DELETED)
    assert len(rows) == 1
    meta = rows[0].metadata_json
    assert rows[0].resource_id == workflow_id
    assert meta["operation"] == "workflow_deleted"
    assert meta["name"] == "triage"
    assert meta["revision"] == 1


async def test_delete_missing_workflow_writes_nothing(client, session, seeded_identity):
    response = await client.delete("/api/v1/workflows/nope")
    assert response.status_code == 404
    assert await _audits(AuditEventType.WORKFLOW_DELETED) == []
