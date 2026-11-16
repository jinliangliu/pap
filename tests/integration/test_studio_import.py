# tests/integration/test_studio_import.py
"""Studio 的统一导入入口：一份文件 → 知识库 / 技能 / 工具。

最要紧的两条：

1. `dry_run` 与真正导入走**同一个**解析，预览里的数字必须等于实际写入的数字；
2. 自动识别可以被显式目标覆盖——同一份 `.md` 既可能是现场规程也可能是技能，
   判错时人要能改，而不是只能改名再传。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.knowledge.index import get_knowledge_index
from app.main import app
from app.models.custom_tool import CustomTool
from app.models.document import Document, DocumentChunk, KnowledgeIndexState
from app.models.rbac import Role, RolePermission, User, UserRole
from app.models.skill import Skill
from app.services.custom_tool_service import unregister_row
from app.tenancy.context import tenant_scope

TENANT = "default"
BASE = "/api/v1/studio"
WRITER_SUBJECT = "studio-writer"
ADMIN_SUBJECT = "platform-admin"

SKILL_MD = """---
name: valve-patrol
display_name: 阀门巡检
description: 按阀位反馈与压差判断是否需要复检
trigger_keywords: [巡检, 阀位]
allowed_tools:
  - knowledge_search
---

# 阀门巡检

1. 读阀位反馈
2. 比对上下游压差
"""

PLAIN_MD = """# 调压箱操作规程

1. 检查进出口阀门
2. 记录进出口压力
"""

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "站点服务", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/stations/{station_id}": {
            "get": {
                "operationId": "studioGetStation",
                "summary": "查站点",
                "parameters": [
                    {
                        "name": "station_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
        "/stations": {
            "post": {
                "operationId": "studioCreateStation",
                "summary": "建站点",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
            }
        },
    },
}


def _client(subject: str = WRITER_SUBJECT, tenant: str = TENANT) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": tenant,
        },
    )


async def _ensure_member(session, subject: str, permissions: tuple[str, ...]) -> None:
    role = await session.scalar(select(Role).where(Role.name == f"studio_{subject}"))

    if role is None:
        role = Role(name=f"studio_{subject}", description=subject)
        session.add(role)
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))
        await session.flush()

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
async def studio(session, seeded_identity):
    with tenant_scope(TENANT):
        await session.execute(delete(Skill))
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    await clear_custom_tools(session)

    # 写权限齐全的普通成员：知识库 + 技能。工具仅平台管理员可用。
    await _ensure_member(
        session,
        WRITER_SUBJECT,
        ("tools:read", "knowledge.read", "knowledge.public", "knowledge.internal",
         "knowledge:write", "skill:write"),
    )
    await _ensure_member(session, "studio-reader", ("tools:read",))
    await session.commit()

    upload_dir = Path(settings.knowledge_upload_dir)
    shutil.rmtree(upload_dir, ignore_errors=True)
    get_knowledge_index().invalidate()

    yield

    get_knowledge_index().invalidate()

    with tenant_scope(TENANT):
        await session.execute(delete(Skill))
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    await clear_custom_tools(session)
    await session.commit()
    shutil.rmtree(upload_dir, ignore_errors=True)


async def clear_custom_tools(session) -> None:
    """清掉本用例建的工具，并保证网关里也注销了。

    只按名字前缀清：别的用例建的工具不该被这里删掉。
    """
    from app.tools.dependencies import get_tool_gateway

    rows = (
        await session.execute(
            select(CustomTool).where(CustomTool.name.like("studio%"))
        )
    ).scalars().all()

    for row in rows:
        unregister_row(get_tool_gateway(), row)
        await session.delete(row)


async def _import(
    http: AsyncClient,
    *,
    name: str,
    body: bytes | str,
    target: str = "auto",
    options: dict | None = None,
    dry_run: bool = False,
):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    data: dict[str, str] = {"target": target, "dry_run": "true" if dry_run else "false"}

    if options is not None:
        data["options"] = json.dumps(options, ensure_ascii=False)

    return await http.post(
        f"{BASE}/import",
        files={"file": (name, payload, "application/octet-stream")},
        data=data,
    )


# --------------------------------------------------------------------------- #
# 识别与目标覆盖
# --------------------------------------------------------------------------- #


async def test_skill_markdown_is_recognised_as_skill(studio):
    async with _client() as http:
        res = await _import(http, name="valve.md", body=SKILL_MD, dry_run=True)

    assert res.status_code == 200
    body = res.json()
    assert body["target"] == "skill"
    assert body["detected_target"] == "skill"
    assert body["preview"]["skills"][0]["name"] == "valve-patrol"


async def test_plain_markdown_goes_to_knowledge(studio):
    async with _client() as http:
        res = await _import(http, name="规程.md", body=PLAIN_MD, dry_run=True)

    assert res.status_code == 200
    body = res.json()
    assert body["target"] == "knowledge"
    assert body["preview"]["chars"] > 0


async def test_explicit_target_overrides_detection(studio):
    """判错了要能改：同一份带 front matter 的文件强制进知识库。"""
    async with _client() as http:
        res = await _import(
            http, name="valve.md", body=SKILL_MD, target="knowledge", dry_run=True
        )

    assert res.status_code == 200
    body = res.json()
    assert body["target"] == "knowledge"
    # 原来判成什么仍然回传，前端要能显示「它自己认成了什么」。
    assert body["detected_target"] == "skill"


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #


async def test_knowledge_dry_run_writes_nothing(studio, session):
    async with _client() as http:
        res = await _import(http, name="规程.md", body=PLAIN_MD, dry_run=True)

    assert res.status_code == 200
    assert res.json()["preview"]["title"] == "规程"

    with tenant_scope(TENANT):
        assert (await session.execute(select(Document))).scalars().all() == []


async def test_knowledge_import_creates_document(studio):
    async with _client() as http:
        res = await _import(
            http,
            name="规程.md",
            body=PLAIN_MD,
            options={"title": "调压箱操作规程", "access_scope": "internal"},
        )

    assert res.status_code == 200
    result = res.json()["result"]
    assert result["action"] == "created"
    assert result["chunk_count"] >= 1
    assert result["title"] == "调压箱操作规程"


async def test_same_file_twice_is_not_duplicated(studio):
    """幂等：同样的内容重传应当返回 unchanged，而不是再入一份。"""
    async with _client() as http:
        first = await _import(http, name="规程.md", body=PLAIN_MD)
        second = await _import(http, name="规程.md", body=PLAIN_MD)

    assert first.json()["result"]["action"] == "created"
    assert second.json()["result"]["action"] == "unchanged"


async def test_reader_cannot_upload_knowledge(studio):
    async with _client("studio-reader") as http:
        res = await _import(http, name="规程.md", body=PLAIN_MD)

    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# 技能
# --------------------------------------------------------------------------- #


async def test_skill_dry_run_previews_and_reports_existence(studio):
    async with _client() as http:
        first = await _import(http, name="valve.md", body=SKILL_MD, dry_run=True)
        assert first.json()["preview"]["skills"][0]["exists"] is False

        await _import(http, name="valve.md", body=SKILL_MD)

        again = await _import(http, name="valve.md", body=SKILL_MD, dry_run=True)

    assert again.json()["preview"]["skills"][0]["exists"] is True


async def test_skill_import_creates_then_updates(studio, session):
    async with _client() as http:
        created = await _import(http, name="valve.md", body=SKILL_MD)
        updated = await _import(
            http, name="valve.md", body=SKILL_MD.replace("比对上下游压差", "比对进出口压差")
        )

    assert created.json()["result"]["created"] == 1
    assert updated.json()["result"]["updated"] == 1

    with tenant_scope(TENANT):
        row = await session.scalar(select(Skill).where(Skill.name == "valve-patrol"))

    assert row is not None
    assert "进出口压差" in row.instruction
    assert row.trigger_keywords == ["巡检", "阀位"]


async def test_skill_name_can_be_overridden_at_import(studio):
    """同一份模板要建多个技能时，靠 options.name 改名，而不是改文件。"""
    async with _client() as http:
        res = await _import(
            http, name="valve.md", body=SKILL_MD, options={"name": "valve-patrol-2"}
        )

    assert res.status_code == 200
    assert res.json()["result"]["created"] == 1


async def test_skill_without_write_permission_is_403(studio):
    async with _client("studio-reader") as http:
        res = await _import(http, name="valve.md", body=SKILL_MD)

    assert res.status_code == 403
    assert "skill:write" in res.json()["detail"]


async def test_broken_skill_markdown_is_400_with_actionable_text(studio):
    async with _client() as http:
        res = await _import(http, name="bad.md", body="---\nname: 巡检\n---\n正文")

    assert res.status_code == 400
    assert "front matter" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


async def test_openapi_dry_run_lists_each_operation(studio):
    async with _client(ADMIN_SUBJECT, "platform") as http:
        res = await _import(
            http,
            name="api.json",
            body=json.dumps(OPENAPI, ensure_ascii=False),
            dry_run=True,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["target"] == "tool"
    assert body["preview"]["count"] == 2
    assert {tool["name"] for tool in body["preview"]["tools"]} == {
        "studiogetstation",
        "studiocreatestation",
    }


async def test_openapi_import_registers_tools(studio, session):
    async with _client(ADMIN_SUBJECT, "platform") as http:
        res = await _import(
            http, name="api.json", body=json.dumps(OPENAPI, ensure_ascii=False)
        )

    assert res.status_code == 200
    result = res.json()["result"]
    assert result["created"] == 2

    with tenant_scope(TENANT):
        names = set(
            (
                await session.execute(
                    select(CustomTool.name).where(CustomTool.name.like("studio%"))
                )
            )
            .scalars()
            .all()
        )

    assert {"studiogetstation", "studiocreatestation"} <= names


async def test_selected_operations_only_write_those(studio, session):
    async with _client(ADMIN_SUBJECT, "platform") as http:
        res = await _import(
            http,
            name="api.json",
            body=json.dumps(OPENAPI, ensure_ascii=False),
            options={"operations": ["studiogetstation"]},
        )

    assert res.status_code == 200
    assert res.json()["result"]["created"] == 1

    with tenant_scope(TENANT):
        names = set(
            (
                await session.execute(
                    select(CustomTool.name).where(CustomTool.name.like("studio%"))
                )
            )
            .scalars()
            .all()
        )

    assert names == {"studiogetstation"}


async def test_tool_import_requires_platform_admin(studio):
    async with _client() as http:
        res = await _import(
            http, name="api.json", body=json.dumps(OPENAPI, ensure_ascii=False)
        )

    assert res.status_code == 403
    assert "平台管理员" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #


async def test_unknown_suffix_is_400(studio):
    async with _client() as http:
        res = await _import(http, name="a.exe", body=b"MZ")

    assert res.status_code == 400
    assert "不支持的文件类型" in res.json()["detail"]


async def test_broken_options_json_is_400(studio):
    async with _client() as http:
        res = await http.post(
            f"{BASE}/import",
            files={"file": ("规程.md", PLAIN_MD.encode(), "text/markdown")},
            data={"target": "auto", "dry_run": "true", "options": "{不是 JSON"},
        )

    assert res.status_code == 400
    assert "options" in res.json()["detail"]


async def test_empty_file_is_400(studio):
    async with _client() as http:
        res = await _import(http, name="空.md", body=b"")

    assert res.status_code == 400


async def test_oversized_file_is_413(studio, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_max_file_bytes", 32)

    async with _client() as http:
        res = await _import(http, name="big.md", body="x" * 200)

    assert res.status_code == 413


async def test_unknown_target_is_400(studio):
    async with _client() as http:
        res = await _import(http, name="规程.md", body=PLAIN_MD, target="whatever")

    assert res.status_code == 400
    assert "未知的导入目标" in res.json()["detail"]


async def test_targets_endpoint_describes_capabilities(studio):
    async with _client() as http:
        res = await http.get(f"{BASE}/import/targets")

    assert res.status_code == 200
    body = res.json()
    values = {item["value"] for item in body["targets"]}
    assert values == {"auto", "knowledge", "skill", "tool"}
    assert body["is_platform_admin"] is False
