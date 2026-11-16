# tests/unit/test_studio_admin_tools.py
"""构建助手第二组工具：技能、自定义工具、知识库。

这组用例守三条约束：

1. **凭据进不来**：写工具收到 `token` / `api_key` 这类字段必须直接拒绝，
   而不是「存起来以后再说」——那条值会经过对话、上下文、日志与审批卡；
2. **平台级配置只有平台管理员能改**：工具表是全局的，改一处影响所有租户；
3. **落库的变更都要留资源级审计**：助手会话不在对话台里，审计是唯一追溯源。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.agent import Agent
from app.models.audit import AuditEventType, AuditLog
from app.models.custom_tool import CustomTool
from app.models.mcp_registered import McpRegisteredTool
from app.models.observability import ObsTrace
from app.models.skill import Skill
from app.services import agent_service, skill_service
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools, studio_tools
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
        "scopes": ("tools:read", "agent:write", "skill:write", "knowledge:write"),
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean_db(session, seeded_identity):
    for model in (Agent, Skill, CustomTool, McpRegisteredTool, ObsTrace):
        await session.execute(delete(model))
    await session.commit()
    yield
    for model in (Agent, Skill, CustomTool, McpRegisteredTool, ObsTrace):
        await session.execute(delete(model))
    await session.commit()


async def _audits_of(resource_id: str) -> list[AuditLog]:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog)
                .where(AuditLog.resource_id == resource_id)
                .order_by(AuditLog.created_at)
            )
            return list(rows.all())


# ---------------- 凭据 ----------------


@pytest.mark.parametrize(
    "field", ["token", "api_key", "accessToken", "password", "headers"]
)
async def test_write_tools_reject_credential_fields(field):
    """任何一层出现凭据字段名就拒绝，并指向安全输入。"""
    arguments = {"kind": "http", "mode": "create", "name": "probe", "url": "https://x.test"}

    if field == "headers":
        # 请求头是最容易被顺手塞进 Authorization 的地方。
        arguments["headers"] = {"Authorization": "Bearer abc"}
    else:
        arguments[field] = "s3cret"

    result = await studio_admin_tools.studio_tool_save(_ctx(), arguments)

    assert result.success is False
    assert "安全输入" in (result.message or "")
    assert "s3cret" not in (result.message or "")


async def test_skill_save_also_rejects_credentials():
    """技能正文是自由文本，但参数里的凭据字段仍然要挡。"""
    result = await studio_admin_tools.studio_skill_save(
        _ctx(), {"mode": "create", "name": "cs", "api_key": "abc"}
    )

    assert result.success is False
    assert "安全输入" in (result.message or "")


async def test_credential_ref_is_allowed():
    """`credential_ref` 是引用不是密钥，拦住它只会逼人去贴明文。"""
    assert studio_admin_tools._guard_credentials({"credential_ref": "OPS_TOKEN"}) is None


# ---------------- 平台级配置的身份门槛 ----------------


async def test_tool_save_requires_platform_admin():
    result = await studio_admin_tools.studio_tool_save(
        _ctx(is_platform_admin=False),
        {"kind": "http", "mode": "create", "name": "probe", "url": "https://x.test"},
    )

    assert result.success is False
    assert "平台管理员" in (result.message or "")


async def test_python_tools_are_read_only():
    """Python 工具是代码注册的，没有运行时入口——必须明确说做不到。"""
    result = await studio_admin_tools.studio_tool_save(
        _ctx(is_platform_admin=True), {"kind": "python", "mode": "create", "name": "probe"}
    )

    assert result.success is False
    assert "代码" in (result.message or "")


# ---------------- 技能 ----------------


async def test_skill_save_creates_and_audits():
    result = await studio_admin_tools.studio_skill_save(
        _ctx(),
        {
            "mode": "create",
            "name": "report-weekly",
            "display_name": "周报",
            "description": "把本周工单整理成周报",
            "instruction": "先取工单，再按模块归并。",
            "trigger_keywords": ["周报"],
        },
    )

    assert result.success is True, result.message

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await skill_service.get_by_name(db, "report-weekly")
            assert row is not None
            assert row.display_name == "周报"
            skill_id = str(row.id)

    audits = await _audits_of(skill_id)
    assert [a.event_type for a in audits] == [AuditEventType.ADMIN_ACTION]
    assert audits[0].resource_type == "skill"
    assert audits[0].metadata_json["via"] == "studio_assistant"
    assert audits[0].metadata_json["tool_name"] == "studio_skill_save"


async def test_skill_save_cannot_grant_privileged_tools():
    """技能的白名单也不能夹带 `studio_*`：那是绕过审批的后门。"""
    result = await studio_admin_tools.studio_skill_save(
        _ctx(),
        {
            "mode": "create",
            "name": "sneaky",
            "allowed_tools": ["studio_agent_save"],
        },
    )

    assert result.success is False
    assert "studio_agent_save" in (result.message or "")


async def test_skill_delete_reports_referencing_agents():
    """删掉还在被引用的技能要说出来，否则表现为「配了却不用」。"""
    await studio_admin_tools.studio_skill_save(
        _ctx(), {"mode": "create", "name": "report-weekly", "display_name": "周报"}
    )

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name="ops",
                display_name="运行值班",
                allowed_skills=["report-weekly"],
                actor_user_id="actor-1",
            )
            await db.commit()

    result = await studio_admin_tools.studio_skill_delete(
        _ctx(), {"name": "report-weekly", "confirm_name": "report-weekly"}
    )

    assert result.success is True, result.message
    assert "运行值班" in (result.message or "")

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            assert await skill_service.get_by_name(db, "report-weekly") is None


async def test_skill_delete_needs_exact_name():
    await studio_admin_tools.studio_skill_save(
        _ctx(), {"mode": "create", "name": "report-weekly"}
    )

    result = await studio_admin_tools.studio_skill_delete(
        _ctx(), {"name": "report-weekly", "confirm_name": "report"}
    )

    assert result.success is False
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            assert await skill_service.get_by_name(db, "report-weekly") is not None


# ---------------- 知识库（不落盘的拒绝分支） ----------------


async def test_knowledge_add_rejects_unknown_scope():
    result = await studio_admin_tools.studio_knowledge_add(
        _ctx(), {"title": "手册", "content": "正文", "access_scope": "secret"}
    )

    assert result.success is False
    assert "分级" in (result.message or "")


async def test_knowledge_add_rejects_scope_above_permission():
    """confidential 要 knowledge.confidential，没有就拒绝——与上传路由同一口径。"""
    result = await studio_admin_tools.studio_knowledge_add(
        _ctx(), {"title": "手册", "content": "正文", "access_scope": "confidential"}
    )

    assert result.success is False
    assert "confidential" in (result.message or "")


async def test_knowledge_add_rejects_empty_content():
    result = await studio_admin_tools.studio_knowledge_add(
        _ctx(), {"title": "手册", "content": "   "}
    )

    assert result.success is False


# ---------------- 读：运行与 Trace ----------------


async def test_describe_runs_reports_empty_window_honestly():
    """没有记录时要说明「可能被关掉了」，而不是断言「没跑过」。"""
    result = await studio_tools.studio_describe(_ctx(), {"resource": "run"})

    assert result.success is True
    assert "没有" in (result.message or "")
    assert "OBS_ENABLED" in (result.message or "")


async def test_describe_trace_requires_id():
    result = await studio_tools.studio_describe(_ctx(), {"resource": "trace"})

    assert result.success is False
    assert "trace_id" in (result.message or "")


async def test_describe_history_requires_agent():
    result = await studio_tools.studio_describe(_ctx(), {"resource": "history"})

    assert result.success is False
    assert "标识" in (result.message or "")


async def test_describe_runs_does_not_count_pending_approval_as_failure():
    """「在等确认」不是失败。

    早期写法是 `status != "ok"` 就算失败，于是助手会对着一条正常流程说
    「昨天失败了 1 次」——这种错最容易被当真，因为它在报告一件坏消息。
    """
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            db.add(
                ObsTrace(
                    trace_id="t-pending",
                    user_id="u1",
                    started_at=now - timedelta(minutes=2),
                    status="awaiting_approval",
                )
            )
            db.add(
                ObsTrace(
                    trace_id="t-failed",
                    user_id="u1",
                    started_at=now - timedelta(minutes=1),
                    status="error",
                    error_category="llm_timeout",
                )
            )
            await db.commit()

    result = await studio_tools.studio_describe(_ctx(), {"resource": "run", "limit": 5})

    assert result.success is True, result.message
    assert result.result["failed"] == 1
    assert "其中失败 1 次" in (result.message or "")
