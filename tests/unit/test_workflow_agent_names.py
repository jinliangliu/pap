# tests/unit/test_workflow_agent_names.py
"""图里的角色名：填标识或显示名都该对上。

这组用例来自线上一次真实对话：用户说「通用助手」，构建助手把同一个问题
（「「查规程」这一步由谁来？」）又问了一遍。根因不是模型不认这四个字，是它
不确定这四个字能不能填进图里——而答案本来就在平台这边（`agents` 表里
`name=assistant, display_name=通用助手`）。

所以两条规矩：**图里存标识**（运行时按标识取 Agent），**入口认显示名**
（用户和模型都按人话说）。重名的显示名不猜：两个「运维」是两张不同的边界，
按名字挑一个等于替用户决定了权限。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.workflow import Workflow, WorkflowRun
from app.orchestration.definition import compile_definition
from app.services import workflow_service
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"

ONE_STEP = {
    "entry": "check",
    "nodes": [
        {"id": "check", "agent": "通用助手", "task": "查规程：{{input.text}}", "next": []}
    ],
}


def _row(name: str, display: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, display_name=display, status=AgentStatus.ACTIVE)


# ---------------------------------------------------------------- 纯函数


def test_display_name_maps_to_the_identifier():
    rows = [_row("assistant", "通用助手"), _row("ops", "运维")]

    assert workflow_service.resolve_agent_name(rows, "通用助手") == "assistant"
    # 标识本身照样能填，大小写与前后空白不影响。
    assert workflow_service.resolve_agent_name(rows, "ops") == "ops"
    assert workflow_service.resolve_agent_name(rows, "OPS") == "ops"
    assert workflow_service.resolve_agent_name(rows, "  通用助手  ") == "assistant"
    # 对不上就是 None，不猜一个最像的。
    assert workflow_service.resolve_agent_name(rows, "巡检") is None


def test_identifier_wins_when_it_collides_with_another_display_name():
    # `ops` 是某个智能体的标识，同时是另一个的显示名：标识优先，否则
    # 「填 ops」会随插入顺序变成两个人。
    rows = [_row("ops", "运维"), _row("x", "ops")]

    assert workflow_service.resolve_agent_name(rows, "ops") == "ops"
    # 那个显示名等于别人标识的智能体，也不会被顺手改名。
    assert workflow_service.resolve_agent_name(rows, "x") == "x"


def test_duplicate_display_names_are_not_guessed():
    # 两个「运维」的工具、可写、转交深度都可能不同，按名字挑一个就是替用户
    # 决定权限。宁可报「不存在」并列出标识。
    rows = [_row("ops_day", "运维"), _row("ops_night", "运维")]

    assert workflow_service.resolve_agent_name(rows, "运维") is None
    assert workflow_service.resolve_agent_name(rows, "ops_day") == "ops_day"


def test_resolve_rewrites_nodes_and_parallel_branches():
    plan = compile_definition(
        {
            "entry": "a",
            "nodes": [
                {
                    "id": "a",
                    "agent": "通用助手",
                    "task": "查 {{input.text}}",
                    "next": [{"when": None, "to": "b"}],
                },
                {
                    "id": "b",
                    "kind": "parallel",
                    "task": "",
                    "branches": [{"id": "one", "agent": "通用助手", "task": "再查一遍"}],
                    "next": [],
                },
            ],
        }
    )

    resolved, renames = workflow_service.resolve_plan_agents(plan, [_row("assistant", "通用助手")])

    assert resolved.node("a").agent == "assistant"
    assert resolved.node("b").branches[0].agent == "assistant"
    # 换过谁要能说出来：草稿与日志都靠这份名单解释「卡上那个人是哪来的」。
    assert ("a", "通用助手", "assistant") in renames


def test_resolve_leaves_unknown_names_alone():
    plan = compile_definition(ONE_STEP)

    resolved, renames = workflow_service.resolve_plan_agents(plan, [])

    # 不认识的名字原样留着，由「谁不在」那一步带着可用名单报出来。
    assert resolved.node("check").agent == "通用助手"
    assert renames == []


# ---------------------------------------------------------------- 落库


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=TENANT,
        user_id="actor-1",
        thread_id="thread-1",
        run_id="run-1",
        request_id="req-1",
        roles=("dev",),
        scopes=("tools:read", "agent:write"),
    )


async def _seed(*specs: tuple[str, str]) -> None:
    from app.services import agent_service

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            for name, display in specs:
                if name == "assistant":
                    # 系统保留名，只能走惰性创建（它本来就是 `name=assistant`、
                    # `display_name=通用助手` 的那一个）。
                    await agent_service.ensure_default(db, tenant_id=TENANT)
                    continue
                await agent_service.create_agent(
                    db,
                    tenant_id=TENANT,
                    name=name,
                    display_name=display,
                    status=AgentStatus.ACTIVE,
                )
            await db.commit()


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    for model in (WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()
    yield
    for model in (WorkflowRun, Workflow, Agent):
        await session.execute(delete(model))
    await session.commit()


async def test_save_stores_the_identifier_not_the_display_name():
    await _seed(("assistant", "通用助手"))

    row = await workflow_service.save_workflow(
        tenant_id=TENANT,
        user_id="actor-1",
        name="check-rules",
        display_name="查规程",
        description=None,
        definition=ONE_STEP,
    )

    # 存进去的必须是标识：运行期 `agent_invoker.invoke(target=...)` 按标识取 Agent。
    assert row.definition["nodes"][0]["agent"] == "assistant"


async def test_draft_says_which_identifier_it_settled_on():
    await _seed(("assistant", "通用助手"))

    result = await studio_admin_tools.studio_workflow_draft(_ctx(), {"definition": ONE_STEP})

    # 起草就该过：显示名对不上不是「图错了」，是平台该自己解决的事。
    assert result.result["ok"] is True
    # 图上从此写 assistant，用户说的是「通用助手」——不说清就成了凭空换人。
    assert "通用助手 → assistant" in result.message
    assert result.result["graph"]["nodes"][0]["agent"] == "assistant"


async def test_unknown_agent_hint_carries_display_names():
    await _seed(("ops", "运维"))

    result = await studio_admin_tools.studio_workflow_draft(
        _ctx(),
        {
            "definition": {
                "entry": "check",
                "nodes": [
                    {"id": "check", "agent": "inspector", "task": "查一下", "next": []}
                ],
            }
        },
    )

    message = result.result["errors"][0]["message"]
    assert "inspector" in message
    # 带着显示名列候选：省掉一次「先 studio_describe 看一眼」的往返。
    assert "ops（运维）" in message
