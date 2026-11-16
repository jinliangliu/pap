# tests/integration/test_skills_api.py
"""技能接口与注入链路。

三条必须守住的：

1. 只读身份能看不能改（否则「规划中」的页面变成人人可改的提示词入口）；
2. 白名单只收窄——技能里写一个当前身份没有的工具，不会让它变得可调用；
3. 命中之后，system prompt 里确实有技能段，工具目录里确实少了东西。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.agents.planner import set_planner_for_tests
from app.agents.schemas import ActionPlan
from app.main import app
from app.models.rbac import Role, RolePermission, User, UserRole
from app.models.skill import Skill
from app.services import skill_service
from app.tenancy.context import tenant_scope

TENANT = "default"
BASE = "/api/v1/skills"


def _client_for(subject: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": TENANT,
        },
    )


async def _ensure_role(session, name: str, permissions: tuple[str, ...]) -> Role:  # noqa: ANN001
    role = await session.scalar(select(Role).where(Role.name == name))
    if role is None:
        role = Role(name=name, description=name)
        session.add(role)
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))
        await session.flush()
    return role


async def _ensure_member(session, *, subject: str, role: Role) -> None:  # noqa: ANN001
    user = await session.scalar(select(User).where(User.external_subject == subject))
    if user is None:
        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()
    exists = await session.scalar(
        select(UserRole).where(UserRole.tenant_id == TENANT, UserRole.user_id == user.id)
    )
    if exists is None:
        session.add(UserRole(tenant_id=TENANT, user_id=user.id, role_id=role.id))
        await session.flush()


@pytest.fixture
async def skill_fixture(session, seeded_identity):
    await session.execute(delete(Skill))

    member_role = await _ensure_role(session, "skill_member", ("tools:read",))
    writer_role = await _ensure_role(session, "skill_writer", ("tools:read", "skill:write"))
    await _ensure_member(session, subject="skill-a", role=member_role)
    await _ensure_member(session, subject="skill-w", role=writer_role)
    await session.commit()

    yield

    await session.execute(delete(Skill))
    await session.commit()


async def test_member_can_read_but_not_write(skill_fixture):
    async with _client_for("skill-a") as http:
        listing = await http.get(BASE)
        assert listing.status_code == 200
        assert listing.json()["can_write"] is False

        denied = await http.post(BASE, json={"name": "patrol", "display_name": "巡检"})
        assert denied.status_code == 403


async def test_create_list_and_patch(skill_fixture):
    async with _client_for("skill-w") as http:
        created = await http.post(
            BASE,
            json={
                "name": "patrol",
                "display_name": "巡检比对",
                "description": "比对附件与工单",
                "instruction": "1. 识别附件\n2. 查工单",
                "trigger_keywords": ["巡检", "比对"],
            },
        )
        assert created.status_code == 201
        item = created.json()
        assert item["name"] == "patrol"
        assert item["allowed_tools"] is None
        assert item["missing_tools"] == []

        patched = await http.patch(
            f"{BASE}/{item['id']}",
            json={"enabled": False, "trigger_keywords": ["巡检"]},
        )
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False
        assert patched.json()["trigger_keywords"] == ["巡检"]


async def test_duplicate_name_conflicts(skill_fixture):
    async with _client_for("skill-w") as http:
        assert (await http.post(BASE, json={"name": "patrol"})).status_code == 201
        again = await http.post(BASE, json={"name": "patrol"})
        assert again.status_code == 409


async def test_bad_name_is_400(skill_fixture):
    async with _client_for("skill-w") as http:
        bad = await http.post(BASE, json={"name": "巡检"})
        assert bad.status_code == 400


async def test_name_is_not_editable(skill_fixture):
    """改名等于换技能：接口层直接拒绝这个字段，而不是默默忽略。"""
    async with _client_for("skill-w") as http:
        created = await http.post(BASE, json={"name": "patrol"})
        item = created.json()
        renamed = await http.patch(f"{BASE}/{item['id']}", json={"name": "other"})
        assert renamed.status_code == 422


async def test_recall_reports_match_and_tool_count(skill_fixture):
    async with _client_for("skill-w") as http:
        await http.post(
            BASE,
            json={
                "name": "patrol",
                "display_name": "巡检比对",
                "instruction": "先识别附件。",
                "trigger_keywords": ["巡检"],
                "allowed_tools": ["get_platform_time", "not_a_real_tool"],
            },
        )

        hit = await http.post(f"{BASE}/recall", json={"message": "帮我巡检一下"})
        assert hit.status_code == 200
        body = hit.json()
        assert body["matched"] is True
        assert body["name"] == "patrol"
        assert body["matched_by"] == "keyword"
        assert body["keyword"] == "巡检"
        # 不可用的工具不进结果，也不该出现在「可用工具」里。
        assert body["tools"] == ["get_platform_time"]
        assert "1 个当前身份不可用" in body["note"]

        explicit = await http.post(f"{BASE}/recall", json={"message": "/patrol 看看"})
        assert explicit.json()["matched_by"] == "explicit"

        miss = await http.post(f"{BASE}/recall", json={"message": "今天天气怎么样"})
        assert miss.json()["matched"] is False


async def test_disabled_skill_never_matches(skill_fixture):
    async with _client_for("skill-w") as http:
        created = await http.post(BASE, json={"name": "patrol", "trigger_keywords": ["巡检"]})
        await http.patch(f"{BASE}/{created.json()['id']}", json={"enabled": False})
        res = await http.post(f"{BASE}/recall", json={"message": "帮我巡检"})
        assert res.json()["matched"] is False


async def test_delete_then_404(skill_fixture):
    async with _client_for("skill-w") as http:
        created = await http.post(BASE, json={"name": "patrol"})
        item = created.json()
        assert (await http.delete(f"{BASE}/{item['id']}")).status_code == 200
        assert (await http.get(f"{BASE}/{item['id']}")).status_code == 404


async def test_export_and_import_roundtrip(skill_fixture):
    async with _client_for("skill-w") as http:
        await http.post(BASE, json={"name": "patrol", "display_name": "巡检"})
        exported = await http.get(f"{BASE}/export")
        assert exported.status_code == 200
        payload = exported.json()
        assert payload["version"] == 1
        assert payload["skills"][0]["name"] == "patrol"

        listing = (await http.get(BASE)).json()
        for item in listing["items"]:
            assert (await http.delete(f"{BASE}/{item['id']}")).status_code == 200
        imported = await http.post(f"{BASE}/import", json=payload)
        assert imported.status_code == 200
        assert imported.json()["created"] == 1
        assert (await http.get(BASE)).json()["total"] == 1


class _CapturingPlanner:
    def __init__(self, plan: ActionPlan) -> None:
        self.plan = plan
        self.messages: list[list[tuple[str, str]]] = []

    async def ainvoke(self, messages):  # noqa: ANN001
        self.messages.append(messages)
        return self.plan


async def test_skill_is_injected_and_narrows_tools(client, session, seeded_identity):
    """命中之后：system 里有技能段，payload 里的工具只剩白名单。"""
    with tenant_scope(TENANT):
        await skill_service.create_skill(
            session,
            name="patrol",
            display_name="巡检比对",
            description="比对附件与工单",
            instruction="先识别附件，再按站点查工单。",
            trigger_keywords=["巡检"],
            allowed_tools=["get_platform_time"],
        )
        await session.commit()

    planner = _CapturingPlanner(ActionPlan(action="chat", response="好的。"))
    set_planner_for_tests(planner)
    try:
        res = await client.post(
            "/api/v1/chat",
            json={"thread_id": "thread-skill-inject", "message": "帮我巡检一下"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "completed"

        system, human = planner.messages[0]
        assert system[0] == "system"
        assert "## 当前技能：巡检比对" in system[1]
        assert "可用工具已被限定为：get_platform_time。" in system[1]

        payload = json.loads(human[1])
        assert [t["tool_name"] for t in payload["available_tools"]] == ["get_platform_time"]
    finally:
        set_planner_for_tests(None)


async def test_no_skill_means_untouched_catalog(client, session, seeded_identity):
    planner = _CapturingPlanner(ActionPlan(action="chat", response="好的。"))
    set_planner_for_tests(planner)
    try:
        res = await client.post(
            "/api/v1/chat",
            json={"thread_id": "thread-skill-none", "message": "今天周几"},
        )
        assert res.status_code == 200
        system, human = planner.messages[0]
        assert "## 当前技能" not in system[1]
        assert len(json.loads(human[1])["available_tools"]) > 1
    finally:
        set_planner_for_tests(None)
