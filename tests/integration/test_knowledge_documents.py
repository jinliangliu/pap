# tests/integration/test_knowledge_documents.py
"""知识库的文档管理闭环：上传 → 检索 → 下架 → 重建。

这一轮修的核心缺陷是「入库只有全量替换」：页面上传一份文档会把该租户
所有切片删掉重插，而目录同步又会把上传的文档当成「目录里没有的脏数据」清掉。
所以最要紧的一条回归是 `test_directory_sync_spares_uploaded_documents`。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.knowledge.index import get_knowledge_index
from app.main import app
from app.models.document import Document, DocumentChunk, KnowledgeIndexState
from app.models.rbac import Role, RolePermission, User, UserRole
from app.tenancy.context import tenant_scope

TENANT = "default"
BASE = "/api/v1/knowledge"
WRITER_SUBJECT = "kb-writer"

DOC_MARKDOWN = """# 阀门停机规程

停机前必须确认三件事：阀位反馈、下游压力、上下游隔离。

## 压力阈值

保压读数低于 0.30 MPa 视为异常，需要复核后再决定是否恢复供气。
"""


def _client(subject: str = WRITER_SUBJECT, tenant: str = TENANT) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-Authenticated-Subject": subject,
            "X-Authenticated-Tenant": tenant,
        },
    )


async def _ensure_writer(session) -> None:
    role = await session.scalar(select(Role).where(Role.name == "kb_writer"))

    if role is None:
        role = Role(name="kb_writer", description="知识库维护者")
        session.add(role)
        await session.flush()

        for permission in (
            "knowledge.read",
            "knowledge.public",
            "knowledge.internal",
            "knowledge:write",
            "tools:read",
        ):
            session.add(RolePermission(role_id=role.id, permission=permission))

        await session.flush()

    user = await session.scalar(select(User).where(User.external_subject == WRITER_SUBJECT))

    if user is None:
        user = User(
            external_subject=WRITER_SUBJECT,
            display_name=WRITER_SUBJECT,
            enabled=True,
        )
        session.add(user)
        await session.flush()

    exists = await session.scalar(
        select(UserRole).where(UserRole.tenant_id == TENANT, UserRole.user_id == user.id)
    )

    if exists is None:
        session.add(UserRole(tenant_id=TENANT, user_id=user.id, role_id=role.id))

    await session.flush()


@pytest.fixture
async def kb(session, seeded_identity):
    with tenant_scope(TENANT):
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    await _ensure_writer(session)
    await session.commit()

    knowledge_dir = Path(settings.knowledge_dir)
    upload_dir = Path(settings.knowledge_upload_dir)
    shutil.rmtree(upload_dir, ignore_errors=True)
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    get_knowledge_index().invalidate()

    yield

    get_knowledge_index().invalidate()

    with tenant_scope(TENANT):
        await session.execute(delete(DocumentChunk))
        await session.execute(delete(Document))
        await session.execute(delete(KnowledgeIndexState))

    await session.commit()
    shutil.rmtree(upload_dir, ignore_errors=True)

    for path in knowledge_dir.glob("*"):
        if path.is_file():
            path.unlink()


async def _upload(http: AsyncClient, name: str = "停机规程.md", body: str = DOC_MARKDOWN):
    return await http.post(
        f"{BASE}/documents",
        files={"file": (name, body.encode("utf-8"), "text/markdown")},
        data={"title": "停机规程", "access_scope": "internal"},
    )


async def test_upload_then_search_then_delete(kb):
    async with _client() as http:
        created = await _upload(http)
        assert created.status_code == 201, created.text

        item = created.json()
        assert item["title"] == "停机规程"
        assert item["status"] == "ready"
        assert item["source_type"] == "upload"
        assert item["access_scope"] == "internal"
        assert item["chunk_count"] >= 1
        assert item["indexed_at"] is not None
        assert item["reindexable"] is True

        listing = (await http.get(f"{BASE}/documents")).json()
        assert listing["total"] == 1
        assert listing["by_source"] == {"upload": 1}
        assert listing["can_write"] is True
        assert ".pdf" in listing["allowed_suffixes"]

        # 检索必须能立刻命中：索引指纹是 5 秒轮询，写完不主动失效就会查不到。
        found = (await http.post(f"{BASE}/search", json={"query": "停机前要确认什么"})).json()
        assert found["found"] is True
        assert any(c["title"] == "停机规程" for c in found["citations"])

        deleted = await http.delete(f"{BASE}/documents/{item['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == 1

        # 下架后立刻不可检索：检索层只读 status='ready' 的切片。
        gone = (await http.post(f"{BASE}/search", json={"query": "停机前要确认什么"})).json()
        assert gone["found"] is False

        after = (await http.get(f"{BASE}/documents")).json()
        assert after["total"] == 0


async def test_detail_returns_chunks(kb):
    async with _client() as http:
        item = (await _upload(http)).json()

        detail = (await http.get(f"{BASE}/documents/{item['id']}")).json()
        assert detail["chunk_total"] == item["chunk_count"]
        assert len(detail["chunks"]) == item["chunk_count"]
        assert "阀位反馈" in detail["chunks"][0]["content"]
        assert detail["chunks"][0]["is_active"] is True

        missing = await http.get(f"{BASE}/documents/{'0' * 8}-0000-0000-0000-000000000000")
        assert missing.status_code == 404


async def test_upload_requires_write_permission(kb):
    # 默认测试身份只有 knowledge.read / internal，没有 knowledge:write。
    async with _client(subject="test-user") as http:
        res = await _upload(http)
        assert res.status_code == 403
        assert "knowledge:write" in res.json()["detail"]

        listing = (await http.get(f"{BASE}/documents")).json()
        assert listing["can_write"] is False


async def test_confidential_upload_needs_matching_scope(kb):
    async with _client() as http:
        res = await http.post(
            f"{BASE}/documents",
            files={"file": ("机密.md", b"# x\n\nsecret", "text/markdown")},
            data={"access_scope": "confidential"},
        )
        assert res.status_code == 403
        assert "knowledge.confidential" in res.json()["detail"]


async def test_rejects_secret_like_name_and_unsupported_type(kb):
    async with _client() as http:
        secret = await http.post(
            f"{BASE}/documents",
            files={"file": ("id_rsa.md", b"PRIVATE KEY", "text/markdown")},
        )
        assert secret.status_code == 400
        assert "禁止入库" in secret.json()["detail"]

        binary = await http.post(
            f"{BASE}/documents",
            files={"file": ("logo.png", b"\x89PNG", "image/png")},
        )
        assert binary.status_code == 400
        assert "不支持的文件类型" in binary.json()["detail"]


async def test_directory_sync_spares_uploaded_documents(kb):
    """回归：目录同步曾经会删光租户所有文档，把上传的资料一起带走。"""
    knowledge_dir = Path(settings.knowledge_dir)
    (knowledge_dir / "notes").mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "notes" / "审批规则.md").write_text(
        "# 审批规则\n\n写操作需要二次确认，审批有效期 600 秒。",
        encoding="utf-8",
    )

    async with _client() as http:
        uploaded = (await _upload(http)).json()

        first = (await http.post(f"{BASE}/reindex")).json()
        assert first["created"] == 1
        assert first["deleted"] == 0

        listing = (await http.get(f"{BASE}/documents")).json()
        kinds = {item["id"]: item["source_type"] for item in listing["items"]}
        assert kinds[uploaded["id"]] == "upload"
        assert sorted(kinds.values()) == ["file", "upload"]

        # 再同步一次：内容没变，不该重建。重建会重算切片，也白白 bump 版本。
        second = (await http.post(f"{BASE}/reindex")).json()
        assert second["unchanged"] == 1
        assert second["created"] == 0
        assert second["deleted"] == 0

        # 目录里的文件删掉后再同步：只下架 file 那份。
        (knowledge_dir / "notes" / "审批规则.md").unlink()
        third = (await http.post(f"{BASE}/reindex")).json()
        assert third["deleted"] == 1

        remaining = (await http.get(f"{BASE}/documents")).json()
        assert remaining["total"] == 1
        assert remaining["items"][0]["id"] == uploaded["id"]


async def test_reindex_document_uses_original_text(kb):
    async with _client() as http:
        item = (await _upload(http)).json()

        res = await http.post(f"{BASE}/documents/{item['id']}/reindex")
        assert res.status_code == 200
        assert res.json()["status"] == "ready"
        assert res.json()["indexed_at"] is not None


async def test_reindex_fails_cleanly_when_source_is_gone(kb):
    async with _client() as http:
        item = (await _upload(http)).json()

        for path in Path(settings.knowledge_upload_dir).rglob("*"):
            if path.is_file():
                path.unlink()

        res = await http.post(f"{BASE}/documents/{item['id']}/reindex")
        assert res.status_code == 409
        assert "原文已不存在" in res.json()["detail"]

        # 失败不等于下架：旧切片仍然可以检索，状态标成 failed 让人看见。
        detail = (await http.get(f"{BASE}/documents/{item['id']}")).json()
        assert detail["document"]["status"] == "failed"
        assert detail["document"]["reindexable"] is False


async def test_cross_tenant_cannot_see_documents(kb):
    async with _client() as http:
        item = (await _upload(http)).json()

    async with _client(subject="other-user", tenant="tenant-b") as other:
        listing = (await other.get(f"{BASE}/documents")).json()
        assert listing["total"] == 0

        detail = await other.get(f"{BASE}/documents/{item['id']}")
        assert detail.status_code == 404

        # 检索也必须隔离：tenant-b 的索引里没有这份文档。
        found = (await other.post(f"{BASE}/search", json={"query": "停机前要确认什么"})).json()
        assert found["found"] is False


async def test_reupload_same_content_is_idempotent(kb):
    """同一份内容传两次不该变成两份文档。

    早期每次上传都生成新的 logical_id，于是同一份资料在库里出现两次，
    检索结果重复、切片数翻倍；用户看到的是「怎么传了一遍又一遍」。
    """
    async with _client() as http:
        first = (await _upload(http)).json()
        second = (await _upload(http, name="停机规程-副本.md")).json()

    assert second["id"] == first["id"]
    assert second["chunk_count"] == first["chunk_count"]

    async with _client() as http:
        listing = (await http.get(f"{BASE}/documents")).json()

    assert listing["total"] == 1


async def test_readding_deleted_document_creates_new_row(kb):
    """下架后重新上传是新建，不是「复用已删除的那一行」。"""
    async with _client() as http:
        first = (await _upload(http)).json()
        await http.delete(f"{BASE}/documents/{first['id']}")
        again = (await _upload(http)).json()

        listing = (await http.get(f"{BASE}/documents")).json()

    assert again["id"] != first["id"]
    assert again["status"] == "ready"
    assert listing["total"] == 1


async def test_sync_logging_survives_info_level(kb, caplog):
    """`extra` 的键不能撞 LogRecord 的保留字段。

    线上日志级别是 INFO，撞了会在 `makeRecord` 里抛
    `KeyError: Attempt to overwrite 'created' in LogRecord`，接口直接 500。
    测试默认 WARNING 不会构造 record，所以必须显式把级别调上来才测得到——
    这个坑正是这么漏过去的。
    """
    import logging

    knowledge_dir = Path(settings.knowledge_dir)
    (knowledge_dir / "日志用.md").write_text("# 标题\n\n正文。", encoding="utf-8")

    caplog.set_level(logging.INFO, logger="app.knowledge.documents")

    async with _client() as http:
        res = await http.post(f"{BASE}/reindex")

    assert res.status_code == 200, res.text
    assert res.json()["created"] == 1
    assert any("知识目录已同步" in record.message for record in caplog.records)


async def test_detail_exposes_real_section_titles(kb):
    """引用不再只说「第 1 段」。"""
    async with _client() as http:
        item = (await _upload(http)).json()
        detail = (await http.get(f"{BASE}/documents/{item['id']}")).json()

    assert [chunk["section"] for chunk in detail["chunks"]] == [
        "阀门停机规程",
        "阀门停机规程 / 压力阈值",
    ]


async def test_chunker_version_forces_rebuild_of_legacy_rows(kb, session):
    """老行没有 chunker_version，必须重建，不能被增量判定当成 unchanged。

    内容与切片数都没变、只有切法变了的场景，是增量同步最容易漏的一类。
    漏掉的表现是「升级完 section 还是第 N 段，怎么同步都不变」。
    """
    from sqlalchemy import select

    knowledge_dir = Path(settings.knowledge_dir)
    (knowledge_dir / "规程.md").write_text(DOC_MARKDOWN, encoding="utf-8")

    async with _client() as http:
        first = (await http.post(f"{BASE}/reindex")).json()
        assert first["created"] == 1

        # 第二次不动内容 → 正常的 unused/unchanged 路径要生效。
        stable = (await http.post(f"{BASE}/reindex")).json()
        assert stable["unchanged"] == 1
        assert stable["created"] == stable["updated"] == 0

        with tenant_scope(TENANT):
            rows = (await session.execute(select(Document))).scalars().all()
            assert len(rows) == 1
            meta = dict(rows[0].metadata_json or {})
            meta.pop("chunker_version", None)
            rows[0].metadata_json = meta
            await session.commit()

        # 抹掉版本后，即便哈希与切片数都相同也必须重建。
        rebuilt = (await http.post(f"{BASE}/reindex")).json()
        assert rebuilt["updated"] == 1
        assert rebuilt["unchanged"] == 0

        detail = (await http.get(f"{BASE}/documents/{str(rows[0].id)}")).json()
        assert detail["chunks"][1]["section"] == "阀门停机规程 / 压力阈值"


async def test_sync_migrates_outdated_upload_documents(kb, session):
    """切片器升级必须覆盖上传文档，不能只重建目录里的那批。

    目录同步按设计不碰上传文档，于是「同步过了」看起来万事大吉，
    实际上传的 78 切片规范还停在旧规则，引用一律显示「第 N 段」。
    """
    async with _client() as http:
        item = (await _upload(http)).json()

        # 造一行旧数据：没有版本号、section 还是「第 N 段」。
        with tenant_scope(TENANT):
            row = (await session.execute(select(Document))).scalars().one()
            meta = dict(row.metadata_json or {})
            meta.pop("chunker_version", None)
            row.metadata_json = meta

            result = await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == row.id)
            )
            chunks = result.scalars().all()
            for chunk in chunks:
                chunk_meta = dict(chunk.metadata_json or {})
                chunk_meta["section"] = f"第 {chunk.chunk_index + 1} 段"
                chunk.metadata_json = chunk_meta
            await session.commit()

        first = (await http.post(f"{BASE}/reindex")).json()
        assert first["migrated"] == 1
        assert first["deleted"] == 0, "迁移不能顺手把上传文档删掉"

        detail = (await http.get(f"{BASE}/documents/{item['id']}")).json()
        assert [chunk["section"] for chunk in detail["chunks"]] == [
            "阀门停机规程",
            "阀门停机规程 / 压力阈值",
        ]

        # 幂等：第二次不能反复重切。
        second = (await http.post(f"{BASE}/reindex")).json()
        assert second["migrated"] == 0


async def test_sync_survives_upload_with_missing_source(kb, session):
    """原文丢了的上传文档不该让整个同步 500，旧切片要继续可用。"""
    async with _client() as http:
        item = (await _upload(http)).json()

        with tenant_scope(TENANT):
            row = (await session.execute(select(Document))).scalars().one()
            meta = dict(row.metadata_json or {})
            meta.pop("chunker_version", None)
            row.metadata_json = meta
            await session.commit()

        for path in Path(settings.knowledge_upload_dir).rglob("*"):
            if path.is_file():
                path.unlink()

        response = await http.post(f"{BASE}/reindex")
        assert response.status_code == 200
        body = response.json()
        assert body["migrated"] == 0
        assert "原文缺失" in body["message"]

        # 不标 failed、不下架：旧切片仍然能检索。
        assert (await http.get(f"{BASE}/documents/{item['id']}")).status_code == 200


async def test_search_exposes_rerank_metadata(kb, monkeypatch):
    """验证页要能区分「模型判定全不相关」和「重筛故障回落」。"""

    async def fake(prompt: str, *, timeout: float) -> str:
        return '{"keep": []}'

    monkeypatch.setattr("app.knowledge.rerank._ask_model", fake)

    async with _client() as http:
        await _upload(http)
        response = await http.post(f"{BASE}/search", json={"query": "阀门停机"})

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["citations"] == []
    assert body["rerank"]["applied"] is True
    assert body["rerank"]["dropped"] >= 1


async def test_search_limit_controls_citation_count(kb):
    """回归：/search 的 limit 以前被接口吞掉，评测脚本无法测 Recall@k。"""
    bodies = (
        "# 阀门巡检\n\n每日巡检阀门外观与铭牌。\n",
        "# 阀门保养\n\n阀门润滑每季度执行一次。\n",
        "# 阀门台账\n\n阀门编号登记在台账里。\n",
    )

    async with _client() as http:
        for index, body in enumerate(bodies):
            response = await _upload(http, name=f"阀门资料{index}.md", body=body)
            assert response.status_code == 201

        one = (await http.post(f"{BASE}/search", json={"query": "阀门", "limit": 1})).json()
        three = (await http.post(f"{BASE}/search", json={"query": "阀门", "limit": 3})).json()

    assert one["found"] is True
    assert len(one["citations"]) == 1
    assert len(three["citations"]) == 3
