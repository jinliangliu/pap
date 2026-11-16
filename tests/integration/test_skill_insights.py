# tests/integration/test_skill_insights.py
"""技能的 P1：命中统计、模板复制、观测按技能筛。

三条口径必须钉住，否则「命中次数」会变成一个自己骗自己的数字：

1. 一次对话只记一次命中——`plan_request` 会因为「工具回来再规划」跑多次；
2. 召回测试不记账——调指令的人一天点几十次，会把真实使用量完全盖掉；
3. 模板复制出来的技能默认停用——复制不等于上线。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.agents.nodes import _record_skill_hit
from app.agents.planner import set_planner_for_tests
from app.agents.schemas import ActionPlan
from app.main import app
from app.models.observability import ObsSpan, ObsTrace
from app.models.rbac import Role, RolePermission, User, UserRole
from app.models.skill import Skill, SkillHit
from app.services import skill_service
from app.tenancy.context import tenant_scope

TENANT = "default"
BASE = "/api/v1/skills"


WRITER_SUBJECT = "skill-writer"


def _client(subject: str = "test-user") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": TENANT,
        },
    )


class _CapturingPlanner:
    def __init__(self, plan: ActionPlan) -> None:
        self.plan = plan
        self.calls = 0

    async def ainvoke(self, messages):  # noqa: ANN001
        self.calls += 1
        return self.plan


@pytest.fixture
async def skill_fixture(session, seeded_identity):
    await session.execute(delete(SkillHit))
    await session.execute(delete(Skill))
    await session.commit()

    # 默认测试身份没有 skill:write，写操作要一个独立的作者身份。
    role = await session.scalar(select(Role).where(Role.name == "skill_writer"))
    if role is None:
        role = Role(name="skill_writer", description="技能作者")
        session.add(role)
        await session.flush()
        for permission in ("tools:read", "skill:write"):
            session.add(RolePermission(role_id=role.id, permission=permission))
        await session.flush()

    user = await session.scalar(select(User).where(User.external_subject == WRITER_SUBJECT))
    if user is None:
        user = User(external_subject=WRITER_SUBJECT, display_name=WRITER_SUBJECT, enabled=True)
        session.add(user)
        await session.flush()
    if (
        await session.scalar(
            select(UserRole).where(UserRole.tenant_id == TENANT, UserRole.user_id == user.id)
        )
        is None
    ):
        session.add(UserRole(tenant_id=TENANT, user_id=user.id, role_id=role.id))
        await session.flush()
    await session.commit()

    with tenant_scope(TENANT):
        await skill_service.create_skill(
            session,
            name="patrol",
            display_name="巡检比对",
            instruction="先识别附件，再比对工单。",
            trigger_keywords=["巡检"],
            allowed_tools=["get_platform_time"],
        )
        await session.commit()

    yield

    await session.execute(delete(SkillHit))
    await session.execute(delete(Skill))
    await session.commit()


async def test_chat_hit_shows_in_list_and_detail(skill_fixture):
    planner = _CapturingPlanner(ActionPlan(action="chat", response="好的。"))
    set_planner_for_tests(planner)
    try:
        async with _client() as http:
            before = (await http.get(BASE)).json()
            assert before["items"][0]["hits_7d"] == 0
            assert before["hits_window_days"] == 7

            res = await http.post(
                "/api/v1/chat",
                json={"thread_id": "thread-hit-1", "message": "帮我巡检一下"},
            )
            assert res.status_code == 200
            assert res.json()["status"] == "completed"

            after = (await http.get(BASE)).json()
            item = after["items"][0]
            assert item["hits_7d"] == 1
            assert item["last_hit_at"] is not None

            detail = (await http.get(f"{BASE}/{item['id']}")).json()
            assert detail["hits_7d"] == 1
            assert len(detail["recent_hits"]) == 1
            hit = detail["recent_hits"][0]
            assert hit["matched_by"] == "keyword"
            assert hit["keyword"] == "巡检"
            # 存的是内部线程 id（租户:用户:客户端 id），客户端 id 在末尾。
            assert hit["thread_id"].endswith("thread-hit-1")
    finally:
        set_planner_for_tests(None)


async def test_recall_never_counts_as_hit(skill_fixture):
    """召回测试是调试工具，不是使用量。"""
    async with _client() as http:
        for _ in range(3):
            hit = await http.post(f"{BASE}/recall", json={"message": "帮我巡检"})
            assert hit.json()["matched"] is True

        listing = (await http.get(BASE)).json()
        assert listing["items"][0]["hits_7d"] == 0


async def test_same_run_is_recorded_once(session, skill_fixture):
    """同一轮里的重规划不该重复计数。"""
    with tenant_scope(TENANT):
        skill = await skill_service.get_by_name(session, "patrol")
        assert skill is not None
        state = {
            "tenant_id": TENANT,
            "user_id": "u-1",
            "thread_id": "thread-dup",
            "run_id": f"run-{uuid.uuid4().hex[:8]}",
        }

        await _record_skill_hit(state, skill, "keyword", "巡检")
        await _record_skill_hit(state, skill, "keyword", "巡检")

        rows = (await session.execute(select(SkillHit).where(SkillHit.skill_id == skill.id))).all()
        assert len(rows) == 1


async def test_hits_outside_window_are_not_counted(session, skill_fixture):
    with tenant_scope(TENANT):
        skill = await skill_service.get_by_name(session, "patrol")
        assert skill is not None

        await skill_service.record_hit(session, skill=skill, matched_by="keyword", keyword="巡检")
        old = await skill_service.record_hit(
            session, skill=skill, matched_by="keyword", keyword="巡检"
        )
        # 8 天前：窗口是 7 天，这条不该进计数。
        old.created_at = datetime.now(UTC) - timedelta(days=8)
        await session.commit()

        counts = await skill_service.counts_since(session)
        assert counts.get(skill.id) == 1


async def test_templates_list_and_copy(skill_fixture):
    async with _client(WRITER_SUBJECT) as http:
        templates = await http.get(f"{BASE}/templates")
        assert templates.status_code == 200
        items = templates.json()
        assert {item["key"] for item in items} == {"patrol-compare", "report-format"}
        # 模板不会凭空出现在技能列表里。
        assert (await http.get(BASE)).json()["total"] == 1

        copied = await http.post(f"{BASE}/templates/patrol-compare")
        assert copied.status_code == 201
        body = copied.json()
        assert body["name"] == "patrol-compare"
        assert body["source"] == "template"
        # 复制不等于上线：先停着，改完再启用。
        assert body["enabled"] is False

        again = await http.post(f"{BASE}/templates/patrol-compare")
        assert again.status_code == 409

        unknown = await http.post(f"{BASE}/templates/not-a-template")
        assert unknown.status_code == 404


async def test_observability_filters_by_skill(session, skill_fixture):
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))

    owner = await session.scalar(select(User).where(User.external_subject == "test-user"))
    assert owner is not None
    user_id = str(owner.id)

    now = datetime.now(UTC)
    traces = [
        ObsTrace(
            tenant_id=TENANT,
            trace_id=uuid.uuid4().hex,
            user_id=user_id,
            started_at=now - timedelta(minutes=3),
            status="ok",
            skill_name="patrol",
            attrs={},
        ),
        ObsTrace(
            tenant_id=TENANT,
            trace_id=uuid.uuid4().hex,
            user_id=user_id,
            started_at=now - timedelta(minutes=2),
            status="ok",
            skill_name=None,
            attrs={},
        ),
    ]
    session.add_all(traces)
    await session.commit()

    async with _client() as http:
        filtered = await http.get("/api/v1/observability/traces?hours=24&skill=patrol")
        assert filtered.status_code == 200
        body = filtered.json()
        assert body["total"] == 1
        assert body["items"][0]["skill_name"] == "patrol"

        all_rows = await http.get("/api/v1/observability/traces?hours=24")
        assert all_rows.json()["total"] == 2

        overview = await http.get("/api/v1/observability/overview?hours=24")
        by_skill = {item["key"]: item["count"] for item in overview.json()["by_skill"]}
        assert by_skill.get("patrol") == 1


async def test_trace_detail_carries_skill_insight(session, skill_fixture):
    """详情页的「Skill 选择」：注入情况来自 span，命中次数来自 skill_hits。

    这两件事必须分开看——技能改过正文之后，历史 trace 显示的仍应是当时的长度；
    而命中次数是持续的计数，不受观测保留期影响。
    """
    await session.execute(delete(ObsSpan))
    await session.execute(delete(ObsTrace))

    owner = await session.scalar(select(User).where(User.external_subject == "test-user"))
    assert owner is not None
    user_id = str(owner.id)
    now = datetime.now(UTC)
    trace_id = uuid.uuid4().hex

    with tenant_scope(TENANT):
        skill = await skill_service.get_by_name(session, "patrol")
        assert skill is not None
        await skill_service.record_hit(
            session,
            skill=skill,
            matched_by="keyword",
            keyword="巡检",
            trace_id=trace_id,
            user_id=user_id,
        )

    session.add_all(
        [
            ObsTrace(
                tenant_id=TENANT,
                trace_id=trace_id,
                user_id=user_id,
                started_at=now - timedelta(minutes=2),
                duration_ms=1800,
                status="ok",
                skill_name="patrol",
                attrs={},
            ),
            ObsSpan(
                tenant_id=TENANT,
                trace_id=trace_id,
                span_id="d" * 16,
                parent_span_id=None,
                seq=0,
                name="llm.call",
                kind="llm",
                started_at=now - timedelta(minutes=2),
                duration_ms=900,
                status="ok",
                content=None,
                attrs={
                    "purpose": "plan",
                    "skill.name": "patrol",
                    "skill.matched_by": "keyword",
                    "skill.chars": 12,
                    "skill.limit": 8000,
                    "skill.tools_before": 27,
                    "skill.tools_after": 1,
                    "skill.truncated": False,
                    "tools.count": 1,
                    "tools.names": "get_platform_time",
                },
            ),
        ]
    )
    await session.commit()

    try:
        async with _client() as http:
            body = (await http.get(f"/api/v1/observability/traces/{trace_id}")).json()

        insight = body["skill"]
        assert insight["name"] == "patrol"
        assert insight["matched_by"] == "keyword"
        assert insight["keyword"] == "巡检"
        assert insight["recorded"] is True
        assert insight["hits_7d"] == 1
        assert insight["instruction_chars"] == 12
        assert insight["instruction_limit"] == 8000
        assert insight["tools_before"] == 27
        assert insight["tools_after"] == 1
        assert insight["exists"] is True
        assert insight["enabled"] is True
        # 工具清单挂在 llm.call 上，前端「准备工具」直接用，不再查注册表。
        assert body["spans"][0]["attrs"]["tools.names"] == "get_platform_time"

        # 删掉技能后：统计随级联清空，但这一轮的注入记录还要能看。
        with tenant_scope(TENANT):
            await skill_service.delete_skill(session, skill)
        await session.commit()

        async with _client() as http:
            gone = (await http.get(f"/api/v1/observability/traces/{trace_id}")).json()
        assert gone["skill"]["name"] == "patrol"
        assert gone["skill"]["exists"] is False
        assert gone["skill"]["hits_7d"] == 0
        assert gone["skill"]["tools_after"] == 1
    finally:
        await session.execute(delete(ObsSpan))
        await session.execute(delete(ObsTrace))
        await session.commit()


async def test_patch_response_keeps_hit_count(session, skill_fixture):
    """保存后的响应必须带真实命中数。

    `_item` 的 `hits` 默认 0 是给「新建」准备的；更新路径沿用它，
    响应体就会说「近 7 天 0 次」——一个存在 2 次的技能被保存一次之后
    数字凭空归零，比不显示更糟。控制台目前保存后会重新拉列表，
    所以没暴露出来；任何按响应体回填的客户端都会踩到。
    """
    with tenant_scope(TENANT):
        skill = await skill_service.get_by_name(session, "patrol")
        assert skill is not None
        await skill_service.record_hit(
            session,
            skill=skill,
            matched_by="keyword",
            keyword="巡检",
        )
        await session.commit()
        skill_id = str(skill.id)

    async with _client(WRITER_SUBJECT) as http:
        res = await http.patch(f"{BASE}/{skill_id}", json={"display_name": "巡检比对 v2"})
        assert res.status_code == 200
        body = res.json()
        assert body["display_name"] == "巡检比对 v2"
        assert body["hits_7d"] == 1
        assert body["last_hit_at"] is not None
