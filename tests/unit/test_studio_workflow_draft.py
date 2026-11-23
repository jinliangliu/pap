# tests/unit/test_studio_workflow_draft.py
"""起草工具：只回答「能不能存」，不写库。

这组用例守三件事：

1. **不落库**。起草与保存拆开的全部价值在这里：保存要过确认卡、进库、涨 revision，
   起草必须一个都不做，否则模型仍然只能赌一把写完整张图。断言方式是查
   `workflows` 表的行数——不是看返回值里没写「已保存」。
2. **错误要带节点**。只说「图不成立」等于让模型重写整份，而重写最容易漏节点。
3. **与保存同一份口径**。角色存在性、启用状态都由 `plan_agents` /
   `AGENT_STATUS_TEXT` 判，起草说能存而保存被拒，比不校验更糟。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.workflow import Workflow, WorkflowRun
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools
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

# 条件边之后没有兜底边：命中不了时流程静默死掉，编译期就该拒。
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


async def _seed_agents(*specs: tuple[str, AgentStatus]) -> None:
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


async def _workflow_rows() -> int:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            return int(await db.scalar(select(func.count()).select_from(Workflow)) or 0)


async def _draft(definition: dict, name: str | None = None):
    arguments: dict = {"definition": definition}
    if name:
        arguments["name"] = name
    return await studio_admin_tools.studio_workflow_draft(_ctx(), arguments)


async def test_draft_accepts_a_runnable_graph():
    result = await _draft(TWO_STEP)

    assert result.success is True
    assert result.result["ok"] is True
    # 图要一起给：模型据此判断「是不是用户描述的那个过程」，气泡也能直接画出来。
    assert [n["id"] for n in result.result["graph"]["nodes"]] == [
        "classify",
        "diagnose",
        "answer",
    ]
    assert "步骤：classify（cs） → diagnose（ops） → answer（汇总）" in result.message
    # 起草后必须告诉它下一步是保存，否则它会以为交上来就算建好了。
    assert "studio_workflow_save" in result.message


async def test_draft_does_not_write_anything():
    await _draft(TWO_STEP)

    # 这是整个改动的立论：起草免费。写进库就不再免费，模型会退回「赌一把」。
    assert await _workflow_rows() == 0


async def test_draft_points_at_the_node_that_breaks_the_graph():
    result = await _draft(BROKEN)

    assert result.result["ok"] is False
    error = result.result["errors"][0]
    assert error["node"] == "a"
    assert error["code"] == "WORKFLOW_NO_FALLBACK"
    assert "节点「a」" in result.message
    # 「改这一处、别重发整张图」要写在结果里：模型对工具返回的下一步更服从。
    assert "不用重发整张图" in result.message
    assert await _workflow_rows() == 0


async def test_draft_reports_an_agent_that_does_not_exist():
    result = await _draft(
        {
            "entry": "solo",
            "nodes": [
                {
                    "id": "solo",
                    "agent": "inspector",
                    "task": "查一下 {{input.text}}",
                    "next": [],
                }
            ],
        }
    )

    assert result.result["ok"] is False
    error = result.result["errors"][0]
    assert error["code"] == "WORKFLOW_UNKNOWN_AGENT"
    assert error["node"] == "solo"
    assert "inspector" in error["message"]
    # 形状是对的，问题在库里的东西：这句区分让模型知道「不必改图，去建角色」。
    assert "形状没问题" in result.message


async def test_draft_reports_an_agent_that_is_not_active():
    await _seed_agents(("drafty", AgentStatus.DRAFT))

    result = await _draft(
        {
            "entry": "solo",
            "nodes": [
                {"id": "solo", "agent": "drafty", "task": "查 {{input.text}}", "next": []}
            ],
        }
    )

    assert result.result["ok"] is False
    assert result.result["errors"][0]["code"] == "WORKFLOW_INACTIVE_AGENT"
    assert "草稿" in result.result["errors"][0]["message"]


async def test_draft_lists_the_steps_that_are_allowed_to_write():
    from app.services import agent_service

    # `can_write` 的默认值是 False（比「有写工具」更严的一层），所以必须显式打开：
    # 「允许写」这件事不能靠工具清单猜。
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name="power",
                display_name="POWER",
                status=AgentStatus.ACTIVE,
                can_write=True,
            )
            await db.commit()

    result = await _draft(
        {
            "entry": "act",
            "nodes": [
                {"id": "act", "agent": "power", "task": "关阀 {{input.text}}", "next": []}
            ],
        }
    )

    assert result.result["writable_nodes"] == ["act"]
    assert "允许写的步骤" in result.message
    # 说「允许写」，不说「会审批」：停不停取决于那一轮它决定调什么工具。
    assert "会停下来等审批" in result.message


async def test_draft_mentions_an_existing_name_without_calling_it_an_error():
    await studio_admin_tools.studio_workflow_save(
        _ctx(),
        {
            "mode": "create",
            "name": "triage",
            "display_name": "告警排查",
            "definition": TWO_STEP,
        },
    )

    result = await _draft(TWO_STEP, name="triage")

    # 同名不是图的问题：把它算成 error 会让模型去改图，改一圈才发现是模式选错。
    assert result.result["ok"] is True
    assert "mode=update" in result.message


async def test_draft_rejects_a_definition_that_is_not_an_object():
    result = await studio_admin_tools.studio_workflow_draft(_ctx(), {"definition": []})

    assert result.success is False
    assert "definition 必须是一个对象" in (result.message or "")


def test_draft_is_registered_as_a_read_tool():
    specs = {spec.name: spec for spec in studio_admin_tools.build_studio_admin_tools()}
    spec = specs["studio_workflow_draft"]

    # 读工具：不弹确认、不要求写权限、也没有 verifier（它不写，没什么可回读的）。
    assert spec.requires_approval is False
    assert spec.required_scopes == ()
    assert spec.verifier is None
    assert spec.backup is None
