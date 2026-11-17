# tests/integration/test_memories.py
"""长期记忆接口。

比「接口能返回 200」更该守住的三条：

1. 别人的记忆查不到、改不了、删不掉，且返回 404 而不是 403；
2. 同标题不会变成两条（否则「称呼」会被记十遍，注入时互相打架）；
3. 删除是物理删除——用户删掉的东西不该还能从库里捞回来。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.main import app
from app.models.memory import Memory
from app.models.rbac import Role, RolePermission, User, UserRole

TENANT = "default"
BASE = "/api/v1/memories"


async def _client_for(subject: str) -> AsyncClient:
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


async def _ensure_member(session, *, subject: str, tenant_id: str, role: Role):  # noqa: ANN001
    user = await session.scalar(select(User).where(User.external_subject == subject))
    if user is None:
        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()
    exists = await session.scalar(
        select(UserRole).where(
            UserRole.tenant_id == tenant_id, UserRole.user_id == user.id
        )
    )
    if exists is None:
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role_id=role.id))
        await session.flush()
    return user


@pytest.fixture
async def mem_fixture(session, seeded_identity):
    """两个普通成员 + 一个有 memory:read 的管理员。"""
    # 测试库是 session 级，记忆表不会自动回滚；先清空，
    # 否则上一个用例的记录会被算进这个用例的列表里。
    await session.execute(delete(Memory))

    member_role = await _ensure_role(session, "mem_member", ("tools:read",))
    admin_role = await _ensure_role(
        session, "mem_admin", ("tools:read", "memory:read")
    )

    await _ensure_member(session, subject="mem-a", tenant_id=TENANT, role=member_role)
    await _ensure_member(session, subject="mem-b", tenant_id=TENANT, role=member_role)
    await _ensure_member(
        session, subject="mem-admin", tenant_id=TENANT, role=admin_role
    )

    # 身份解析走新 session，不 commit 就是「未知身份」401。
    await session.commit()

    yield

    await session.execute(delete(Memory))


async def test_create_and_list(mem_fixture):
    async with await _client_for("mem-a") as http:
        created = await http.post(
            BASE,
            json={
                "memory_type": "profile",
                "title": "称呼",
                "content": "希望被称呼为 dylan",
            },
        )
        assert created.status_code == 201
        item = created.json()
        assert item["title"] == "称呼"
        assert item["scope"] == "self"
        # 人自己写的，天然是已确认状态。
        assert item["is_user_confirmed"] is True

        listing = await http.get(BASE)
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == item["id"]


async def test_same_title_updates_instead_of_duplicating(mem_fixture):
    """同标题第二次写入应更新内容，而不是多出一条。

    不这么做的后果：自动抽取跑几个月，「称呼」会攒出十几条不同说法，
    注入时全部进 prompt，模型只能挑一个猜。
    """
    async with await _client_for("mem-a") as http:
        first = await http.post(
            BASE, json={"memory_type": "preference", "title": "回答风格", "content": "简洁"}
        )
        assert first.status_code == 201
        second = await http.post(
            BASE,
            json={"memory_type": "preference", "title": "回答风格", "content": "先结论后依据"},
        )
        assert second.status_code == 201

        body = (await http.get(BASE)).json()
        assert body["total"] == 1
        assert body["items"][0]["content"] == "先结论后依据"
        assert body["items"][0]["id"] == first.json()["id"]


async def test_other_member_cannot_see_or_touch(mem_fixture):
    async with await _client_for("mem-a") as http:
        created = await http.post(
            BASE, json={"title": "内部暗号", "content": "只有 a 知道"}
        )
        memory_id = created.json()["id"]

    async with await _client_for("mem-b") as http:
        listing = (await http.get(BASE)).json()
        assert listing["total"] == 0

        # 看不到、改不了、删不掉，一律 404：403 等于承认这条记录存在。
        assert (await http.patch(f"{BASE}/{memory_id}", json={"title": "改"})).status_code == 404
        assert (await http.delete(f"{BASE}/{memory_id}")).status_code == 404
        assert (await http.post(f"{BASE}/{memory_id}/confirm")).status_code == 404


async def test_delete_is_physical(mem_fixture, session):
    async with await _client_for("mem-a") as http:
        created = await http.post(
            BASE, json={"title": "临时想法", "content": "删了就没了"}
        )
        memory_id = created.json()["id"]

        deleted = await http.delete(f"{BASE}/{memory_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        assert (await http.get(BASE)).json()["total"] == 0

    # 软删会让「删除」变成一次撒谎：记录还在库里，只是界面上看不见。
    remaining = await session.scalar(
        select(func.count()).select_from(Memory).where(Memory.title == "临时想法")
    )
    assert remaining == 0


async def test_edit_marks_confirmed(mem_fixture):
    """人手动改过的内容按已确认算。

    自动抽取的候选会带 is_user_confirmed=false，改一下就转正，
    否则页面上永远挂着「待确认」，用户点久了就不点了。
    """
    async with await _client_for("mem-a") as http:
        created = await http.post(
            BASE, json={"title": "常用站点", "content": "A 站"}
        )
        memory_id = created.json()["id"]

    # 手工塞一条未确认的候选，模拟自动抽取写入的记录。
    async with await _client_for("mem-a") as http:
        patched = await http.patch(
            f"{BASE}/{memory_id}", json={"content": "A 站和 B 站"}
        )
        assert patched.status_code == 200
        assert patched.json()["is_user_confirmed"] is True
        assert patched.json()["content"] == "A 站和 B 站"


async def test_tenant_scope_memory_needs_permission(mem_fixture):
    """团队共享记忆只有带权限的人能写。"""
    async with await _client_for("mem-a") as http:
        denied = await http.post(
            BASE,
            json={"title": "团队约定", "content": "周报周五发", "scope": "tenant"},
        )
        assert denied.status_code == 403

    async with await _client_for("mem-admin") as http:
        allowed = await http.post(
            BASE,
            json={"title": "团队约定", "content": "周报周五发", "scope": "tenant"},
        )
        assert allowed.status_code == 201
        assert allowed.json()["scope"] == "tenant"

    # 普通成员看得到（团队信息本就是共享的），但删不掉。
    async with await _client_for("mem-a") as http:
        listing = (await http.get(BASE)).json()
        assert listing["total"] == 1
        assert listing["can_manage_tenant"] is False
        tenant_item = listing["items"][0]
        assert (await http.delete(f"{BASE}/{tenant_item['id']}")).status_code == 404


async def test_prompt_block_respects_budget():
    """注入块必须有上限。

    没有上限时，一个人记满 200 条，每轮 prompt 就多几千 token，
    账单涨了还查不出原因。
    """
    from app.models.memory import MemoryStatus, MemoryType
    from app.services.memory_service import PROMPT_BLOCK_BUDGET, render_prompt_block

    rows = [
        Memory(
            user_id="u1",
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            title=f"事实{i}",
            content="很长的内容" * 60,
            source="user",
            confidence=1.0,
            is_user_confirmed=True,
            metadata_json={},
            tenant_id=TENANT,
        )
        for i in range(60)
    ]
    block = render_prompt_block(rows)

    assert "其余记忆已省略" in block
    # 预算之外只允许「省略提示」和结尾那句免责说明。
    assert len(block) < PROMPT_BLOCK_BUDGET + 200


async def test_prompt_block_empty_when_no_memory():
    from app.services.memory_service import render_prompt_block

    assert render_prompt_block([]) == ""


async def test_create_works_with_info_logging(mem_fixture):
    """INFO 日志开着时写入也不能炸。

    `logger.info(..., extra={"created": ...})` 会在 Logger.makeRecord 里抛
    KeyError——`created` 是 LogRecord 的保留字段。而这条路径默认不可见：
    root logger 通常是 WARNING，本地测试一路 201，线上开着 INFO 就变成
    「接口 500、数据其实已提交、日志里连堆栈都没有」。
    """
    import logging

    logger = logging.getLogger("app.api.routes.memories")
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        async with await _client_for("mem-a") as http:
            response = await http.post(
                BASE, json={"title": "日志用例", "content": "写一条看看"}
            )
        assert response.status_code == 201
    finally:
        logger.setLevel(previous)
