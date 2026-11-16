# tests/integration/test_mcp_upload.py
"""MCP 通道的文件上传端点：`POST /mcp/upload`。

这个端点存在的唯一理由是「模型自己搬不了字节」——所以它必须与 REST 上传
**行为一致**：同样的后缀白名单、同样的分级权限、同样的幂等判据。
两处各写一份的下场是差别只出现在其中一条传输上，而那时最难判断是传输坏了
还是入库坏了。所以这里既有「能传」的正例，也有「和 REST 判据一致」的负例。

鉴权不在这里测（`BearerGate` 自己有一套），这里注入的是门槛解析完之后
那个 `Identity` —— 也就是上传处理器真正能看到的东西。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.auth.dependencies import Identity
from app.knowledge.upload import UploadDenied, UploadRejected, precheck_upload
from app.mcp_server.auth import STATE_KEY
from app.mcp_server.upload import UPLOAD_PATH, handle_upload, is_upload_path
from app.models.document import Document, DocumentChunk, KnowledgeIndexState
from app.models.rbac import Role, RolePermission, User, UserRole
from app.tenancy.context import tenant_scope

TENANT = "default"
WRITER = "mcp-uploader"
READER = "mcp-reader"

DOC = """# 非民用智能燃气表 时钟电池要求

时钟电池应保证在断电情况下时钟连续走时不少于五年。
"""


def _identity(subject: str, scopes: tuple[str, ...], user_id: str) -> Identity:
    return Identity(
        user_id=user_id,
        tenant_id=TENANT,
        subject=subject,
        display_name=subject,
        roles=(),
        scopes=scopes,
    )


async def _ensure_user(session, subject: str, *, write: bool) -> User:
    role_name = "mcp_writer" if write else "mcp_reader"
    role = await session.scalar(select(Role).where(Role.name == role_name))

    if role is None:
        role = Role(name=role_name, description="上传端点用例")
        session.add(role)
        await session.flush()

        permissions = ["knowledge.read", "knowledge.internal"]
        if write:
            permissions.append("knowledge:write")

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
    return user


@pytest.fixture
async def upload_env(session, seeded_identity):
    with tenant_scope(TENANT):
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    writer = await _ensure_user(session, WRITER, write=True)
    reader = await _ensure_user(session, READER, write=False)
    await session.commit()

    yield {
        "writer": _identity(WRITER, ("knowledge.read", "knowledge.internal", "knowledge:write"), str(writer.id)),
        "reader": _identity(READER, ("knowledge.read", "knowledge.internal"), str(reader.id)),
    }

    with tenant_scope(TENANT):
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    await session.commit()


def _app(identity: Identity | None) -> Any:
    """只装上传处理器，模拟「门槛已通过、身份已放进 scope」之后的状态。"""

    async def application(scope, receive, send):
        if identity is not None:
            scope.setdefault("state", {})[STATE_KEY] = identity
        await handle_upload(scope, receive, send)

    return application


def _client(identity: Identity | None) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_app(identity)),
        base_url="http://testserver",
    )


def test_upload_path_matches_only_its_own_segment():
    assert is_upload_path("/mcp/upload")
    assert is_upload_path("/mcp/upload/")
    # `/mcp/uploader` 不是上传端点：前缀匹配会让它意外走上传处理器。
    assert not is_upload_path("/mcp/uploader")
    assert not is_upload_path("/mcp")
    assert not is_upload_path("/mcp/")
    assert UPLOAD_PATH == "/mcp/upload"


def test_precheck_rejects_unknown_suffix_and_scope():
    identity = _identity(WRITER, ("knowledge:write",), "x")

    with pytest.raises(UploadRejected):
        precheck_upload(identity=identity, filename="a.exe")

    with pytest.raises(UploadRejected):
        precheck_upload(identity=identity, filename="a.md", access_scope="绝密")

    with pytest.raises(UploadRejected):
        precheck_upload(identity=identity, filename="")

    # `.env` 这类名字与目录扫描同一套排除规则。
    with pytest.raises(UploadRejected):
        precheck_upload(identity=identity, filename=".env")


def test_precheck_denies_without_write_permission():
    identity = _identity(READER, ("knowledge.read",), "x")

    with pytest.raises(UploadDenied):
        precheck_upload(identity=identity, filename="a.md")


async def test_upload_creates_document(upload_env):
    async with _client(upload_env["writer"]) as http:
        response = await http.post(
            UPLOAD_PATH,
            files={"file": ("电池要求.md", DOC.encode("utf-8"), "text/markdown")},
            data={"title": "非民用智能燃气表 时钟电池要求", "access_scope": "internal"},
        )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["action"] == "created"
    assert body["document"]["title"] == "非民用智能燃气表 时钟电池要求"
    assert body["document"]["access_scope"] == "internal"
    assert body["document"]["status"] == "ready"
    assert body["document"]["chunk_count"] >= 1
    assert "已入库" in body["message"]


async def test_same_content_twice_is_idempotent(upload_env):
    payload = {"file": ("电池要求.md", DOC.encode("utf-8"), "text/markdown")}

    async with _client(upload_env["writer"]) as http:
        first = await http.post(UPLOAD_PATH, files=payload, data={"title": "电池要求"})
        second = await http.post(UPLOAD_PATH, files=payload, data={"title": "电池要求"})

    assert first.json()["action"] == "created"
    # 同一份正文重复上传不该变成两份文档：那样检索时同一条资料出现两次，
    # 还各占一份切片，且用户看不出哪份是新的。
    assert second.json()["action"] == "unchanged"
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert "没有重复入库" in second.json()["message"]


async def test_upload_denied_without_write_permission(upload_env):
    async with _client(upload_env["reader"]) as http:
        response = await http.post(
            UPLOAD_PATH,
            files={"file": ("a.md", DOC.encode("utf-8"), "text/markdown")},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


async def test_upload_rejects_bad_extension(upload_env):
    async with _client(upload_env["writer"]) as http:
        response = await http.post(
            UPLOAD_PATH,
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "rejected"


async def test_upload_rejects_non_multipart(upload_env):
    async with _client(upload_env["writer"]) as http:
        response = await http.post(UPLOAD_PATH, json={"file": "x"})

    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


async def test_upload_rejects_get(upload_env):
    async with _client(upload_env["writer"]) as http:
        response = await http.get(UPLOAD_PATH)

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


async def test_upload_without_gate_identity_is_401(upload_env):
    # 门槛已经挡过一轮；这里测的是「万一挂载方式变了」仍然 fail-closed，
    # 而不是回落到某个默认身份。
    async with _client(None) as http:
        response = await http.post(
            UPLOAD_PATH,
            files={"file": ("a.md", DOC.encode("utf-8"), "text/markdown")},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
