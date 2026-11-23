# tests/unit/test_workflow_patch.py
"""流程 patch：改一处不必重发整张图。

这组用例守三件事：

1. **合并规则要能被单独测**。`apply_patch` 是纯函数，不碰库——图怎么变、
   哪条边被摘掉、什么情况必须拒绝，全在这里钉住，不必起一个数据库会话。
2. **不许静默改拓扑**。删掉一个节点后，若某一步因此变成终点，这里必须报错。
   流程从「三步走完」变成「停在第二步」不抛任何异常，只会在复盘时被发现。
3. **四个入口用同一份解析**。起草、保存、确认卡、预检都走
   `_graph_from_arguments`：卡上画的那张图必须就是存下去的那张。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.conversation import Conversation, Message
from app.models.workflow import Workflow, WorkflowRun
from app.orchestration.definition import PatchError, apply_patch, compile_definition
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"


def _base() -> dict:
    """三节点图，a 有两条出边（一条兜底、一条条件）。

    用它测「删掉 b 之后 a 还在」：只有一条出边的图删掉后继必然是死路，
    那样测不出「摘掉边」与「拒绝」的区别。
    """
    return {
        "entry": "a",
        "max_nodes": 9,
        "nodes": [
            {
                "id": "a",
                "agent": "cs",
                "task": "查 {{input.text}}",
                "emit": {"field": "level"},
                "next": [
                    {"when": {"key": "level", "op": "eq", "value": "超压"}, "to": "b"},
                    {"when": None, "to": "c"},
                ],
            },
            {"id": "b", "agent": "ops", "task": "处置", "next": [{"when": None, "to": "c"}]},
            {"id": "c", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
        ],
    }


def _ids(graph: dict) -> list[str]:
    return [node["id"] for node in graph["nodes"]]


def _node(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)


def _codes(exc: pytest.ExceptionInfo) -> str:
    assert isinstance(exc.value, PatchError)
    return exc.value.code


# ---------------------------------------------------------------- 纯函数


def test_set_nodes_replaces_by_id_and_appends_the_new_one():
    merged, notes = apply_patch(
        _base(),
        {
            "set_nodes": [
                {"id": "b", "agent": "ops", "task": "按超压关阀", "next": [{"when": None, "to": "c"}]},
                {"id": "d", "kind": "compose", "task": "附：处置记录", "next": []},
            ]
        },
    )

    assert _ids(merged) == ["a", "b", "c", "d"]
    assert _node(merged, "b")["task"] == "按超压关阀"
    assert notes == ["替换节点「b」", "新增节点「d」"]


def test_replacing_a_node_does_not_inherit_its_old_edges():
    """整节点替换：没写 next 就是没有后继，不是「沿用旧的」。

    合并语义会让「我没写 next」同时表示两件相反的事，而这两件事跑起来完全不同。
    """
    merged, _ = apply_patch(_base(), {"set_nodes": [{"id": "b", "agent": "ops", "task": "只改任务"}]})

    assert "next" not in _node(merged, "b")


def test_removing_a_node_strips_the_edges_pointing_at_it():
    merged, notes = apply_patch(_base(), {"remove_nodes": ["b"]})

    assert _ids(merged) == ["a", "c"]
    assert [edge["to"] for edge in _node(merged, "a")["next"]] == ["c"]
    assert "「a」的 next 里去掉了指向「b」的那条" in notes


def test_removing_a_node_that_would_leave_a_dead_end_is_refused():
    base = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agent": "cs", "task": "x", "next": [{"when": None, "to": "b"}]},
            {"id": "b", "kind": "compose", "task": "y", "next": []},
        ],
    }

    with pytest.raises(PatchError) as exc:
        apply_patch(base, {"remove_nodes": ["b"]})

    assert _codes(exc) == "WORKFLOW_PATCH_ORPHAN_EDGE"
    # 报错要指到「哪一步会停下」，否则模型只知道删不掉，不知道该补哪条边。
    assert exc.value.node == "a"


def test_removing_an_unknown_node_is_refused():
    with pytest.raises(PatchError) as exc:
        apply_patch(_base(), {"remove_nodes": ["zzz"]})

    assert _codes(exc) == "WORKFLOW_PATCH_UNKNOWN_NODE"


def test_removing_the_entry_node_requires_a_new_entry():
    with pytest.raises(PatchError) as exc:
        apply_patch(_base(), {"remove_nodes": ["a"]})

    assert _codes(exc) in {"WORKFLOW_PATCH_ORPHAN_EDGE", "WORKFLOW_PATCH_UNKNOWN_NODE"}


def test_unknown_patch_key_is_refused():
    with pytest.raises(PatchError) as exc:
        apply_patch(_base(), {"nodes": []})

    assert _codes(exc) == "WORKFLOW_PATCH_UNKNOWN_FIELD"
    # 文案要说清能写什么，不然下一版还是猜。
    assert "set_nodes" in exc.value.message


def test_patch_can_move_the_entry():
    merged, _ = apply_patch(_base(), {"entry": "b"})

    assert merged["entry"] == "b"


def test_entry_outside_the_graph_is_refused():
    with pytest.raises(PatchError) as exc:
        apply_patch(_base(), {"entry": "zzz"})

    assert _codes(exc) == "WORKFLOW_PATCH_UNKNOWN_NODE"


def test_graph_without_nodes_cannot_be_patched():
    with pytest.raises(PatchError) as exc:
        apply_patch({}, {"remove_nodes": ["a"]})

    assert _codes(exc) == "WORKFLOW_PATCH_NO_BASE"


def test_patch_keeps_the_keys_it_does_not_own():
    merged, _ = apply_patch(_base(), {"remove_nodes": ["b"]})

    # `max_nodes` 这类整图参数不该因为改了一个节点被重置成默认值。
    assert merged["max_nodes"] == 9


def test_patched_graph_still_compiles():
    merged, _ = apply_patch(
        _base(),
        {"set_nodes": [{"id": "b", "agent": "ops", "task": "改为先通风", "next": [{"when": None, "to": "c"}]}]},
    )

    plan = compile_definition(merged)

    assert plan.order == ("a", "b", "c")


# ---------------------------------------------------------------- 工具入口


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


async def _seed_agents() -> None:
    from app.services import agent_service

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            for name in ("cs", "ops"):
                await agent_service.create_agent(
                    db,
                    tenant_id=TENANT,
                    name=name,
                    display_name=name.upper(),
                    status=AgentStatus.ACTIVE,
                )
            await db.commit()


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    for model in (Message, Conversation, WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()
    await _seed_agents()
    yield
    for model in (WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()


async def _save(name: str = "triage", definition: dict | None = None) -> None:
    from app.services import workflow_service

    await workflow_service.save_workflow(
        tenant_id=TENANT,
        user_id="actor-1",
        name=name,
        display_name=name,
        description=None,
        definition=definition or _base(),
    )


async def _row(name: str = "triage"):
    from app.services import workflow_service

    return await workflow_service.get_workflow(TENANT, name)


async def test_draft_applies_the_patch_to_the_saved_graph():
    await _save()

    result = await studio_admin_tools.studio_workflow_draft(
        _ctx(),
        {"name": "triage", "patch": {"set_nodes": [{"id": "b", "agent": "ops", "task": "改为先通风", "next": [{"when": None, "to": "c"}]}]}},
    )

    assert result.result["ok"] is True
    assert "本次改动：替换节点「b」" in result.message
    # 起草仍然不写库：patch 只是把「改完是什么样」算出来给模型看。
    row = await _row()
    assert _node(row.definition, "b")["task"] == "处置"


async def test_draft_refuses_definition_and_patch_together():
    await _save()

    result = await studio_admin_tools.studio_workflow_draft(
        _ctx(), {"name": "triage", "definition": _base(), "patch": {"entry": "b"}}
    )

    assert result.success is False
    assert "只能给一个" in result.message


async def test_patch_without_a_matching_workflow_says_what_to_do():
    result = await studio_admin_tools.studio_workflow_draft(
        _ctx(), {"name": "nope", "patch": {"entry": "b"}}
    )

    assert result.success is False
    assert "没有标识为「nope」的流程" in result.message
    assert "definition" in result.message


async def test_patch_without_a_name_is_refused():
    result = await studio_admin_tools.studio_workflow_draft(_ctx(), {"patch": {"entry": "b"}})

    assert result.success is False
    assert "必须带 name" in result.message


async def test_save_writes_the_merged_graph_and_bumps_the_revision():
    await _save()
    before = await _row()

    result = await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {
            "mode": "update",
            "name": "triage",
            "patch": {"set_nodes": [{"id": "b", "agent": "ops", "task": "改为先通风", "next": [{"when": None, "to": "c"}]}]},
        },
    )

    assert result.success is True
    assert "本次改动：替换节点「b」" in result.message
    after = await _row()
    assert _node(after.definition, "b")["task"] == "改为先通风"
    # 图真的变了才涨修订号：改一处也是一张新图。
    assert int(after.revision) == int(before.revision) + 1


async def test_save_with_a_patch_leaves_the_other_nodes_untouched():
    await _save()
    before = await _row()

    await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {"mode": "update", "name": "triage", "patch": {"remove_nodes": ["b"]}},
    )

    after = await _row()
    assert _ids(after.definition) == ["a", "c"]
    assert _node(after.definition, "a")["task"] == _node(before.definition, "a")["task"]
    assert [edge["to"] for edge in _node(after.definition, "a")["next"]] == ["c"]


async def test_save_refuses_a_patch_that_orphans_a_node():
    """patch 本身拼得上，改出来的图却跑不到某一步——保存必须拒。

    这是 patch 最容易出的事故形态：摘掉一条边看起来只是改了一处，实际把
    后面几步变成了永远不跑的死代码，而它们还在图上、还在确认卡上。
    """
    await _save()
    before = await _row()

    result = await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {
            "mode": "update",
            "name": "triage",
            "patch": {"set_nodes": [{"id": "a", "agent": "cs", "task": "只改任务", "next": [{"when": None, "to": "c"}]}]},
        },
    )

    assert result.success is False
    assert "WORKFLOW_UNREACHABLE_NODE" in result.message
    assert "节点「b」" in result.message
    # 被拒之后库里那份还在，修订号也没动：拒绝不能留下半张图。
    after = await _row()
    assert int(after.revision) == int(before.revision)
    assert _node(after.definition, "b")["task"] == "处置"


async def test_workflow_rows_are_still_counted_through_one_tenant():
    await _save()
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            total = int(await db.scalar(select(func.count()).select_from(Workflow)) or 0)
    assert total == 1


# ---------------------------------------------------------------- 草稿跨轮

async def _seed_draft(definition: dict, name: str | None = None) -> str:
    """把「上一轮起草过的那张图」写进消息元数据。

    这是聊天服务落库时写的形状（`chat_service` 的 `workflow_draft` 键），
    这里按同一形状造数据——用别的形状测出来的通过不算数。
    """
    import uuid as _uuid

    from app.models.conversation import Conversation, Message, MessageRole

    conversation_id = _uuid.uuid4()
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            db.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=TENANT,
                    user_id="actor-1",
                    thread_id=f"studio-thread-{conversation_id}",
                    channel="studio",
                )
            )
            db.add(
                Message(
                    tenant_id=TENANT,
                    conversation_id=conversation_id,
                    role=MessageRole.ASSISTANT,
                    content="这张图能存。",
                    metadata_json={"workflow_draft": {"name": name, "definition": definition}},
                )
            )
            await db.commit()
    return str(conversation_id)


def _draft_ctx(conversation_id: str) -> ToolExecutionContext:
    return _ctx(conversation_id=conversation_id)


async def test_draft_returns_the_graph_for_the_next_turn():
    result = await studio_admin_tools.studio_workflow_draft(_ctx(), {"definition": _base()})

    draft = result.result["workflow_draft"]
    assert [node["id"] for node in draft["definition"]["nodes"]] == ["a", "b", "c"]
    assert draft["name"] is None
    # 下一轮怎么用它，要在正文里说出来：模型对工具刚告诉它的下一步最服从。
    assert "留在本会话里" in result.message


async def test_patch_without_a_name_lands_on_the_draft_from_this_conversation():
    conversation_id = await _seed_draft(_base())

    result = await studio_admin_tools.studio_workflow_draft(
        _draft_ctx(conversation_id),
        {"patch": {"set_nodes": [{"id": "b", "agent": "ops", "task": "改为先通风", "next": [{"when": None, "to": "c"}]}]}},
    )

    assert result.result["ok"] is True
    assert "替换节点「b」" in result.message
    # 必须说清底图来自草稿：否则读者以为改的是库里那张（库里可能还没有）。
    assert "叠在本会话上一版草稿上" in result.message
    ids = [node["id"] for node in result.result["workflow_draft"]["definition"]["nodes"]]
    assert ids == ["a", "b", "c"]


async def test_patch_without_a_name_or_a_draft_says_what_to_do():
    result = await studio_admin_tools.studio_workflow_draft(
        _ctx(conversation_id=""), {"patch": {"entry": "b"}}
    )

    assert result.success is False
    assert "必须带 name" in result.message


async def test_save_can_create_from_a_patched_draft():
    """起草 → 改一处 → 保存：库里本来没有这张图，patch 落在草稿上。"""
    conversation_id = await _seed_draft(_base())

    result = await studio_admin_tools.studio_workflow_save(
        _draft_ctx(conversation_id),
        {
            "mode": "create",
            "name": "triage",
            "patch": {"set_nodes": [{"id": "b", "agent": "ops", "task": "改为先通风", "next": [{"when": None, "to": "c"}]}]},
        },
    )

    assert result.success is True
    row = await _row()
    assert _node(row.definition, "b")["task"] == "改为先通风"
    assert int(row.revision) == 1
