# tests/unit/test_studio_workflow_tools.py
"""构建助手的编排流程工具：增、删、改、查。

这组用例守四件事：

1. **增与改不自动判断**：`mode=create` 撞到已有名字时要拒绝并指向 update，
   否则打错一个字母就会静默改掉另一张图，而审批卡上写的是「新建」；
2. **改图不丢节点**：不传 `definition` 表示「只改说明与启停」，不能把图清空；
3. **图不成立就拒绝**，并指到具体节点——这是 `compile_definition` 的唯一一份校验，
   助手这边不再写第二套口径；
4. **删除要逐字核对**，且说明未完结的运行不受影响（它们冻结了自己的图）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.audit import AuditEventType, AuditLog
from app.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from app.services import workflow_service
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools, studio_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"

TWO_STEP = {
    "entry": "classify",
    "nodes": [
        {
            "id": "classify",
            "agent": "cs",
            "task": "判断类别：{{input.text}}",
            "emit": {"field": "category"},
            "next": [{"when": None, "to": "diagnose"}],
        },
        {
            "id": "diagnose",
            "agent": "ops",
            "task": "按 {{nodes.classify}} 排查",
            "next": [{"when": None, "to": "answer"}],
        },
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}

# 没有兜底边：条件全不命中时流程会静默死掉，必须在保存时就被拒绝。
BROKEN = {
    "entry": "a",
    "nodes": [
        {
            "id": "a",
            "agent": "cs",
            "task": "x",
            "emit": {"field": "k"},
            "next": [{"when": {"key": "k", "op": "eq", "value": "1"}, "to": "b"}],
        },
        {"id": "b", "kind": "compose", "task": "done", "next": []},
    ],
}


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "roles": ("dev",),
        "scopes": ("tools:read", "agent:write"),
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    for model in (WorkflowRun, Workflow):
        await session.execute(delete(model))
    await session.commit()
    yield
    for model in (WorkflowRun, Workflow):
        await session.execute(delete(model))
    await session.commit()


async def _create(name: str = "triage", definition: dict | None = None):
    return await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {
            "mode": "create",
            "name": name,
            "display_name": "告警排查",
            "description": "分类 → 排查 → 汇总",
            "definition": definition or TWO_STEP,
        },
    )


async def _load(name: str = "triage") -> Workflow | None:
    return await workflow_service.get_workflow(TENANT, name)


# ---------------- 增 ----------------


async def test_create_saves_a_runnable_graph():
    result = await _create()

    assert result.success is True
    # 保存即编译：节点数与跳数是从编译结果算的，不是参数里抄的。
    assert result.result["workflow"]["node_count"] == 3
    # 汇总节点不调 Agent，所以只算 2 跳。
    assert result.result["workflow"]["depth_hops"] == 2
    assert "已新建流程" in result.message
    # 触发方式必须写进结果：建完不会用的人下一步一定会问「怎么跑」。
    assert "/workflow triage" in result.message

    row = await _load()
    assert row is not None
    assert row.revision == 1
    assert row.enabled is True


async def test_create_refuses_an_existing_name():
    await _create()

    again = await _create()

    assert again.success is False
    assert "mode=update" in again.message


async def test_create_rejects_a_broken_graph_and_points_at_the_node():
    result = await _create(name="broken", definition=BROKEN)

    assert result.success is False
    assert "WORKFLOW_NO_FALLBACK" in result.message
    # 「第几步错了」比「图错了」好找得多——节点 id 必须出现在给模型看的那句话里。
    assert "节点「a」" in result.message
    assert await _load("broken") is None


@pytest.mark.parametrize("name", ["Bad Name", "bad/name", "-leading"])
async def test_bad_workflow_name_is_rejected(name):
    result = await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "create", "name": name, "definition": TWO_STEP}
    )

    assert result.success is False
    assert "流程名" in result.message or "标识" in result.message


async def test_create_without_definition_is_rejected():
    result = await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "create", "name": "empty"}
    )

    assert result.success is False
    assert "definition" in result.message
    assert await _load("empty") is None


# ---------------- 改 ----------------


async def test_update_without_definition_keeps_the_graph():
    await _create()

    result = await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {"mode": "update", "name": "triage", "display_name": "告警排查（改）"},
    )

    assert result.success is True
    row = await _load()
    assert row is not None
    # 图不能被「只改名字」顺手清空——那是这条路上最容易发生的破坏。
    assert len(row.definition["nodes"]) == 3
    assert row.display_name == "告警排查（改）"
    assert row.description == "分类 → 排查 → 汇总"


async def test_update_keeps_enabled_when_not_passed():
    await _create()
    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "enabled": False}
    )

    # 只改说明、不传 enabled 时必须保持「停用」，否则会悄悄把它重新启用。
    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "description": "改说明"}
    )

    row = await _load()
    assert row is not None
    assert row.enabled is False


async def test_update_missing_workflow_is_rejected():
    result = await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "ghost", "definition": TWO_STEP}
    )

    assert result.success is False
    assert "mode=create" in result.message


async def test_update_bumps_revision():
    await _create()
    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "definition": TWO_STEP}
    )

    row = await _load()
    assert row is not None
    assert row.revision == 2


# ---------------- 删 ----------------


async def test_delete_requires_the_exact_name():
    await _create()

    refused = await studio_admin_tools.studio_workflow_delete(
        _ctx(), {"name": "triage", "confirm_name": "Triage"}
    )
    assert refused.success is False
    assert await _load() is not None

    done = await studio_admin_tools.studio_workflow_delete(
        _ctx(), {"name": "triage", "confirm_name": "triage"}
    )
    assert done.success is True
    assert await _load() is None


async def test_delete_reports_unfinished_runs():
    """删配置不动运行：run 行上冻结了自己的图，所以未完结的 run 照常继续。"""
    await _create()

    async with async_session_factory() as session:
        with tenant_scope(TENANT):
            session.add(
                WorkflowRun(
                    tenant_id=TENANT,
                    workflow_name="triage",
                    revision=1,
                    definition=TWO_STEP,
                    thread_id="thread-1",
                    status=WorkflowRunStatus.PAUSED,
                    nodes_used=1,
                )
            )
            await session.commit()

    done = await studio_admin_tools.studio_workflow_delete(
        _ctx(), {"name": "triage", "confirm_name": "triage"}
    )

    assert done.success is True
    assert done.result["open_runs"] == 1
    assert "不受影响" in done.message


# ---------------- 查 ----------------


async def test_describe_lists_workflows_with_shape():
    await _create()

    result = await studio_tools.studio_describe(_ctx(), {"resource": "workflow"})

    assert result.success is True
    assert result.result["total"] == 1
    assert "triage" in result.message
    assert "3 个节点 / 2 跳" in result.message


async def test_describe_detail_puts_the_graph_in_the_message():
    """图必须进 message：结果渲染器会把嵌套列表削平，模型照着残缺的图改就会炸。"""
    await _create()

    result = await studio_tools.studio_describe(
        _ctx(), {"resource": "workflow", "ref": "triage"}
    )

    assert result.success is True
    # 库里存的是编译后的规范形态（会补 max_chars / max_nodes），所以只断言结构。
    stored = result.result["definition"]
    assert stored["entry"] == "classify"
    assert [node["id"] for node in stored["nodes"]] == ["classify", "diagnose", "answer"]
    assert '"entry": "classify"' in result.message
    # 改图的那句话要连着图一起给，否则模型会自己发明一份新图。
    assert "studio_workflow_save" in result.message


async def test_describe_missing_workflow_says_so():
    result = await studio_tools.studio_describe(
        _ctx(), {"resource": "workflow", "ref": "ghost"}
    )

    assert result.success is False
    assert "ghost" in result.message


# ---------------- 审计 ----------------


async def test_mutations_leave_a_resource_level_audit():
    await _create()
    row = await _load()
    assert row is not None
    workflow_id = str(row.id)

    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "display_name": "改名"}
    )
    await studio_admin_tools.studio_workflow_delete(
        _ctx(), {"name": "triage", "confirm_name": "triage"}
    )

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog)
                # 按资源 id 收窄：别的用例也会往这张表写 workflow 事件。
                .where(AuditLog.resource_id == workflow_id)
                .order_by(AuditLog.created_at)
            )
            events = list(rows.all())

    operations = [row.metadata_json.get("operation") for row in events]
    assert operations == ["workflow_created", "workflow_updated", "workflow_deleted"]
    # 助手会话不在对话台里，审计是唯一能回答「这张图被谁改过」的地方。
    assert all(row.event_type == AuditEventType.ADMIN_ACTION for row in events)
    assert all(row.metadata_json.get("via") == "studio_assistant" for row in events)
