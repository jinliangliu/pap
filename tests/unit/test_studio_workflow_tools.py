# tests/unit/test_studio_workflow_tools.py
"""构建助手的编排流程工具：增、删、改、查。

这组用例守四件事：

1. **增与改不自动判断**：`mode=create` 撞到已有名字时要拒绝并指向 update，
   否则打错一个字母就会静默改掉另一张图，而审批卡上写的是「新建」；
2. **改图不丢节点**：不传 `definition` 表示「只改说明与启停」，不能把图清空；
3. **图不成立就拒绝**，并指到具体节点——这是 `compile_definition` 的唯一一份校验，
   助手这边不再写第二套口径；
4. **删除要逐字核对**，且说明未完结的运行不受影响（它们冻结了自己的图）；
5. **图里的智能体必须是租户里真实存在且启用中的**：用自然语言建流程时模型最容易
   编一个名字出来，存进去之后要等到有人 `/workflow` 触发才失败。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
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
    for model in (WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()
    await _seed_agents()
    yield
    for model in (WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()


async def _seed_agents(*specs: tuple[str, AgentStatus]) -> None:
    """图里引用的智能体要真实存在：保存时会逐个核对（存在 + 启用中）。"""
    from app.services import agent_service

    wanted = specs or (("cs", AgentStatus.ACTIVE), ("ops", AgentStatus.ACTIVE))

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            for name, status in wanted:
                await agent_service.create_agent(
                    db,
                    tenant_id=TENANT,
                    name=name,
                    display_name=name.upper(),
                    status=status,
                )
            await db.commit()


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
    # 节点数与跳数是两个数字，看不出顺序。建完要能一眼核对「先做什么再做什么」。
    assert "步骤：classify → diagnose → answer" in result.message

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
    # 「把 triage 改名成巡检」是最容易踩空的问法：标识不能改。不说清的话，
    # 下一步就是 create 出一张重复的图。
    assert "不能改" in result.message and "display_name" in result.message


async def test_revision_only_moves_when_the_graph_changes():
    """修订号回答的是「那次跑的是哪张图」，不是「这行什么时候被改过」。

    运行记录与 span 上记的就是这个号。改个显示名也让它在飞的 run 换个版本号，
    复盘时就会出现「那次跑的是 v2」而 v2 与 v1 是同一张图。
    """
    await _create()

    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "display_name": "换个名字"}
    )
    row = await _load()
    assert row is not None
    assert row.revision == 1

    # 编辑页会把整份图原样传回：逐字相同同样不算改图。
    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "definition": row.definition}
    )
    assert (await _load()).revision == 1

    changed = json.loads(json.dumps(TWO_STEP))
    changed["nodes"][1]["task"] = "按 {{nodes.classify}} 换一种排查"
    await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "definition": changed}
    )
    assert (await _load()).revision == 2


async def test_create_rejects_an_agent_that_does_not_exist():
    """自然语言建流程最常见的一种坏：模型不知道租户有哪些智能体，编一个名字出来。

    不挡的话图存得进去，一直到有人 `/workflow` 触发才失败——那时配图的人
    早就不在屏幕前了。拒绝理由必须带上可用的标识，否则下一轮它还会编。
    """
    ghost = {
        "entry": "a",
        "nodes": [{"id": "a", "agent": "inspector", "task": "查一下", "next": []}],
    }

    result = await _create(name="ghost-agent", definition=ghost)

    assert result.success is False
    assert "WORKFLOW_UNKNOWN_AGENT" in result.message
    assert "inspector" in result.message
    assert "cs" in result.message
    assert await _load("ghost-agent") is None


async def test_create_rejects_an_inactive_agent():
    await _seed_agents(("drafty", AgentStatus.DRAFT))
    draft = {
        "entry": "a",
        "nodes": [{"id": "a", "agent": "drafty", "task": "查一下", "next": []}],
    }

    result = await _create(name="draft-agent", definition=draft)

    assert result.success is False
    assert "WORKFLOW_INACTIVE_AGENT" in result.message
    assert "草稿" in result.message
    assert await _load("draft-agent") is None


async def test_renaming_still_works_after_an_agent_is_deleted():
    """图没变就不校验智能体。

    否则「删掉一个智能体」会把引用它的流程锁进只读：改名、改说明、停用全都
    被一句「第 2 步指定的智能体不存在」挡回来，而用户想做的根本不是改图。
    """
    await _create()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            await db.execute(delete(Agent).where(Agent.name == "ops"))
            await db.commit()

    result = await studio_admin_tools.studio_workflow_save(
        _ctx(), {"mode": "update", "name": "triage", "display_name": "改名"}
    )

    assert result.success is True
    assert (await _load()).display_name == "改名"


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
    # 删除不自己盖「已验证」的章：那是与删除同一个事务里的自述。它只交出 id，
    # 由 `verify_workflow_deleted` 另起会话回读。
    assert done.verified is False
    assert done.external_operation_id


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
    # 但不要写成工具名：这一行是正文，用户会读到的。
    assert "整份传回来" in result.message and "没传的节点等于删掉了" in result.message
    assert "studio_" not in result.message


async def test_describe_missing_workflow_says_so():
    result = await studio_tools.studio_describe(
        _ctx(), {"resource": "workflow", "ref": "ghost"}
    )

    assert result.success is False
    assert "ghost" in result.message


# ---------------- 确认卡摘要 ----------------

# 带分流的图：`check` 按 level 分流，其余走兜底边。
BRANCHED = {
    "entry": "check",
    "nodes": [
        {
            "id": "check",
            "agent": "cs",
            "task": "查：{{input.text}}",
            "emit": {"field": "level", "values": ["严重", "一般"]},
            "next": [
                {"when": {"key": "level", "op": "eq", "value": "严重"}, "to": "act"},
                {"when": None, "to": "report"},
            ],
        },
        {"id": "act", "agent": "ops", "task": "处置", "next": [{"when": None, "to": "report"}]},
        {"id": "report", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}


async def test_summary_lists_roles_and_branches_without_restating_the_graph():
    """审批人看的不是 JSON：谁做、按什么分流，必须在卡上写清。

    这条挡的是线上出现过的一次：卡片只有「新建或修改编排流程」一行，用户
    直接点了确认，而图里做的事与他说的事并不一致。

    **顺序只写一遍**：图给得出来时正文不再写「步骤：a → b」。图能表达分叉，
    文字不能；两处都写，读的人得先分辨哪一份是新的。
    """
    brief = await studio_admin_tools._summarize_workflow_call(
        _ctx(),
        {
            "mode": "create",
            "name": "patrol",
            "display_name": "早班巡检",
            "definition": BRANCHED,
        },
    )

    assert "新建流程「patrol」（早班巡检）" in brief.text
    assert "分流：check 判 level = 严重 → act；其余 → report" in brief.text
    # 角色行与图同一份名字（显示名优先，退回标识）。
    assert "角色：CS（check）；OPS（act）；1 个汇总节点不调智能体" in brief.text
    # 有图就不再写步骤：同一份顺序出现在文字和图上，等于让审批人读两遍。
    assert "步骤：" not in brief.text

    # 同一份图还要给一张结构图：文字那行丢了分叉（走哪条、其余走哪条），
    # 而人读卡片是扫一遍就点确认的。
    graph = brief.graph
    assert [node["id"] for node in graph["nodes"]] == ["check", "act", "report"]
    assert (graph["edges"][0]["from"], graph["edges"][0]["to"]) == ("check", "act")
    # 兜底边必须带来：缺了它，图上看着像「没有分流」。
    assert any(edge["when"] is None and edge["to"] == "report" for edge in graph["edges"])
    # 边上只写值（「严重」），键留给正文那行「分流：」——两处都说全会挤成一团。
    assert graph["edges"][0]["label"] == "严重"


async def test_summary_is_empty_when_only_metadata_changes():
    """只改显示名时没有图要说，卡片上那一行参数就够了。"""
    brief = await studio_admin_tools._summarize_workflow_call(
        _ctx(), {"mode": "update", "name": "triage", "display_name": "改名"}
    )

    assert brief.text == ""
    # 没有图就没有形状可画：宁可不出图，也不画一张空框。
    assert brief.graph is None


async def test_summary_does_not_pretend_a_broken_graph_is_fine():
    """编译不过就直说，不猜顺序：卡上写的顺序与实际执行不一致，比不写更糟。"""
    brief = await studio_admin_tools._summarize_workflow_call(
        _ctx(), {"mode": "create", "name": "triage", "definition": BROKEN}
    )

    assert "编译不过" in brief.text
    assert "步骤：" not in brief.text
    # 编译不过就不给图：一张"看着能跑"的图，比没有图危险。
    assert brief.graph is None


async def test_preflight_rejects_a_broken_graph_before_the_card():
    """注定被拒的图不该走到「生成审批单」这一步——那是闭眼确认的训练场。"""
    assert await studio_admin_tools._preflight_workflow(_ctx(), {"definition": BROKEN})

    assert (
        await studio_admin_tools._preflight_workflow(_ctx(), {"definition": BRANCHED}) is None
    )
    # 改说明/启停这类没有图的调用不归它管。
    assert await studio_admin_tools._preflight_workflow(_ctx(), {"name": "triage"}) is None


# ---------------- 回读核验 ----------------


async def test_verifier_confirms_the_graph_that_was_saved():
    await _create()

    result = await studio_admin_tools.verify_workflow_saved(
        _ctx(external_operation_id="triage"),
        {"mode": "create", "name": "triage", "definition": TWO_STEP},
    )

    assert result.verified is True
    assert result.result["node_count"] == 3
    assert result.result["revision"] == 1


async def test_verifier_reports_missing_when_nothing_was_written():
    result = await studio_admin_tools.verify_workflow_saved(
        _ctx(external_operation_id="ghost"), {"name": "ghost"}
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_WORKFLOW_MISSING"


async def test_verifier_catches_a_graph_that_is_not_the_one_submitted():
    """库里是另一张图（同名被别处改过）时，批准的是 A、实际是 B，不能算核验通过。"""
    await _create()

    result = await studio_admin_tools.verify_workflow_saved(
        _ctx(external_operation_id="triage"),
        {"name": "triage", "definition": BRANCHED},
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_WORKFLOW_MISMATCH"
    assert result.verification["actual"] == ["answer", "classify", "diagnose"]


async def test_verifier_catches_an_enabled_state_that_did_not_stick():
    await _create()

    result = await studio_admin_tools.verify_workflow_saved(
        _ctx(external_operation_id="triage"),
        {"mode": "update", "name": "triage", "enabled": False},
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_WORKFLOW_STATE"


async def _delete_reference(name: str = "triage") -> str:
    """删一次，返回 handler 交出来的流程 id（和审批链路上拿到的是同一个）。"""
    row = await _load(name)
    assert row is not None
    reference = str(row.id)

    done = await studio_admin_tools.studio_workflow_delete(
        _ctx(), {"name": name, "confirm_name": name}
    )
    assert done.success is True
    assert done.external_operation_id == reference
    return reference


async def test_delete_verifier_confirms_the_row_is_gone():
    await _create()
    reference = await _delete_reference()

    result = await studio_admin_tools.verify_workflow_deleted(
        _ctx(external_operation_id=reference), {"name": "triage"}
    )

    assert result.verified is True
    assert result.result["workflow_id"] == reference
    assert result.verification["check"] == "absent"


async def test_delete_verifier_will_not_take_absence_as_evidence():
    """查不到 ≠ 删掉了。

    `workflows` 挂 RLS：租户上下文没绑上、名字换成另一个、库被换过，查出来都是
    空集且不报错。把空集当成功，这条核验就成了一根永远绿的线——比没有更糟。
    没有删除审计时如实说「核不出结论」，状态留在 EXECUTED。
    """
    await _create()
    await _delete_reference()

    # 这条 id 从来没存在过，因此也不会有删除审计。
    ghost = "11111111-2222-3333-4444-555555555555"
    result = await studio_admin_tools.verify_workflow_deleted(
        _ctx(external_operation_id=ghost), {"name": "triage"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_evidence"


async def test_delete_verifier_catches_a_row_that_is_still_there():
    await _create()
    row = await _load()
    assert row is not None

    result = await studio_admin_tools.verify_workflow_deleted(
        _ctx(external_operation_id=str(row.id)), {"name": "triage"}
    )

    assert result.success is False
    assert result.verified is False
    assert result.error_code == "VERIFY_WORKFLOW_PRESENT"


async def test_delete_verifier_says_so_when_there_is_nothing_to_read():
    result = await studio_admin_tools.verify_workflow_deleted(
        _ctx(), {"name": "triage"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_ref"


# ---------------- 结果里的角色 ----------------


async def test_result_shows_which_agent_runs_each_step():
    """角色要写进结果，不能只写节点 id。

    用户可能点名过某个角色（「让 ops 判断」），图里换成了另一个——只列 id 时
    这种替换在对话里完全看不出来。
    """
    result = await _create()

    assert "角色：CS（classify）；OPS（diagnose）；1 个汇总节点不调智能体" in result.message
    # 结构化那份仍是标识：调用方按标识取 Agent，展示名只活在文字里。
    assert result.result["workflow"]["roles"] == {
        "cs": ["classify"],
        "ops": ["diagnose"],
    }


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


# ---------------- 写前备份 ----------------


async def test_backup_spec_carries_the_previous_graph():
    """保存前留的那份必须是**整张图**，而不是节点数之类的摘要。

    图被覆盖之后，光看「原来 3 个节点」重建不出任何东西；要能重跑那张图，
    原件里必须有 entry、nodes、边与每条边上的条件。
    """
    await _create()

    spec = await studio_admin_tools._backup_workflow(
        _ctx(), {"mode": "update", "name": "triage"}
    )

    assert spec is not None
    assert spec.resource_type == "workflow"
    assert spec.payload["definition"]["entry"] == "classify"
    assert len(spec.payload["definition"]["nodes"]) == 3
    assert spec.payload["revision"] == 1


async def test_backup_is_empty_for_create():
    """新建没有被覆盖的旧状态。返回 None 表示「没有可备的」，不是失败。

    把这两种情况混在一起，新建类写操作会因为「备不了」被拦下——而那是
    这条机制最不该挡住的一类操作。
    """
    spec = await studio_admin_tools._backup_workflow(
        _ctx(), {"mode": "create", "name": "brand-new"}
    )

    assert spec is None


async def test_backup_is_empty_when_the_row_is_gone():
    """update 打到一个不存在的名字上：交给 handler 去报「没有这个流程」。

    备份钩子不替它判断，也不编一份空图——空图会被写成「原来什么都没有」。
    """
    spec = await studio_admin_tools._backup_workflow(
        _ctx(), {"mode": "update", "name": "nothing-here"}
    )

    assert spec is None
