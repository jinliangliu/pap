# tests/integration/test_knowledge_store.py
"""知识索引的存储与热加载。

历史缺陷：索引只存在本地 chunks.json，进程各自持有一份副本。
重新入库后必须重启服务才可见，多实例之间还会出现版本不一致。
现在切片落在 PostgreSQL，进程内只保留按版本号刷新的派生结构。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.knowledge import store
from app.knowledge.index import KnowledgeIndex
from app.knowledge.ingest import rebuild
from app.knowledge.service import answer_from_knowledge

TENANT = "kb-test"


def _record(
    *,
    chunk_id: str,
    document_id: str,
    text: str,
    scope: str = "internal",
    index: int = 0,
    title: str = "测试文档",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "chunk_index": index,
        "text": text,
        "title": title,
        "source_path": f"notes/{document_id}.md",
        "section": f"第 {index + 1} 段",
        "updated_at": "2026-11-02T00:00:00+00:00",
        "content_hash": "sha256:" + "a" * 64,
        "access_scope": scope,
        "document_type": "knowledge_document",
        "tags": [],
    }


async def test_chunks_roundtrip_with_scope_filter(session):
    await store.replace_all(
        session,
        TENANT,
        [
            _record(
                chunk_id="pub-0",
                document_id="pub",
                text="场站压力阈值规范",
                scope="public",
                title="公开规范",
            ),
            _record(
                chunk_id="conf-0",
                document_id="conf",
                text="场站压力阈值机密处置流程",
                scope="confidential",
                title="机密流程",
            ),
        ],
    )
    await session.commit()

    retriever = await KnowledgeIndex().get(session, TENANT, force=True)

    assert retriever is not None
    assert len(retriever) == 2

    documents, citations = retriever.search(
        query="压力阈值",
        allowed_scopes={"public", "internal"},
        min_score=0.0,
    )

    assert {document["title"] for document in documents} == {"公开规范"}
    assert citations[0].source_path == "notes/pub.md"


async def test_index_reloads_when_version_changes(session, monkeypatch):
    """重新入库后，不重启进程也要看到新内容。"""
    monkeypatch.setattr(settings, "knowledge_index_refresh_seconds", 0.0)

    index = KnowledgeIndex()

    await store.replace_all(
        session,
        TENANT,
        [_record(chunk_id="v1-0", document_id="v1", text="压力阈值规范")],
    )
    await session.commit()

    first = await index.get(session, TENANT, force=True)
    assert first is not None
    assert first.search(query="压力", allowed_scopes={"internal"}, min_score=0.0)[0]

    await store.replace_all(
        session,
        TENANT,
        [_record(chunk_id="v2-0", document_id="v2", text="温度阈值规范")],
    )
    await session.commit()

    second = await index.get(session, TENANT)

    assert second is not None
    assert second is not first
    assert second.search(query="温度", allowed_scopes={"internal"}, min_score=0.0)[0]
    assert second.search(query="压力", allowed_scopes={"internal"}, min_score=0.0)[0] == []


async def test_version_is_bumped_on_every_ingest(session):
    await store.replace_all(session, TENANT, [])
    await session.commit()
    first = await store.fingerprint(session, TENANT)

    await store.replace_all(session, TENANT, [])
    await session.commit()
    second = await store.fingerprint(session, TENANT)

    assert first != second


async def test_rebuild_writes_database_not_json(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_index_refresh_seconds", 0.0)

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "gateway.md").write_text(
        "# 工具网关\n\n工具网关负责权限校验与审批。\n",
        encoding="utf-8",
    )
    # 第二份文档用于把 IDF 抬到正数：查询词只出现在其中一份文档时才有正分，
    # 单文档语料下 BM25 的 IDF 为负，会被 min_score 过滤掉。
    (tmp_path / "notes" / "pressure.md").write_text(
        "# 场站压力\n\n场站压力阈值由调压器决定。\n",
        encoding="utf-8",
    )
    # N=2 且 df=1 时 BM25 的 IDF 正好为 0，会被 min_score 过滤；
    # 补第三份文档让 IDF 变正，模拟真实语料规模。
    (tmp_path / "notes" / "alarm.md").write_text(
        "# 告警\n\n告警按等级分派给值班人员。\n",
        encoding="utf-8",
    )

    report = await rebuild(str(tmp_path), tenant_id=TENANT)

    assert report.documents == 3
    assert report.chunks >= 1
    assert report.target.startswith("documents/document_chunks")
    assert report.export_path == ""

    answer = await answer_from_knowledge(
        query="权限校验与审批",
        scopes=("knowledge.read",),
        tenant_id=TENANT,
    )

    assert answer.found
    assert answer.citations


# ---------------- 切片级 ACL ----------------
#
# 关键顺序：ACL 过滤发生在打分**之前**。
# 未授权内容不进入 BM25 候选，也就不可能被模型看到或被引用。


def _acl_record(
    *,
    chunk_id: str,
    text: str,
    visibility: str = "tenant_shared",
    allowed_roles: list[str] | None = None,
    owner_user_id: str | None = None,
    scope: str = "internal",
) -> dict[str, Any]:
    record = _record(chunk_id=chunk_id, document_id=chunk_id, text=text, scope=scope)
    record["visibility"] = visibility
    record["allowed_roles"] = allowed_roles or []
    record["owner_user_id"] = owner_user_id
    return record


async def test_user_private_chunks_are_only_visible_to_owner(session):
    await store.replace_all(
        session,
        "kb-acl",
        [
            _acl_record(
                chunk_id="alice-note",
                text="场站压力阈值：专属记录",
                visibility="user_private",
                owner_user_id="alice",
            ),
            _acl_record(chunk_id="shared-note", text="场站压力阈值：共享记录"),
        ],
    )
    await session.commit()

    retriever = await KnowledgeIndex().get(session, "kb-acl", force=True)
    assert retriever is not None

    as_alice, _ = retriever.search(
        query="场站压力阈值",
        allowed_scopes={"internal"},
        roles={"tester"},
        user_id="alice",
        min_score=0.0,
    )
    as_bob, _ = retriever.search(
        query="场站压力阈值",
        allowed_scopes={"internal"},
        roles={"tester"},
        user_id="bob",
        min_score=0.0,
    )
    anonymous, _ = retriever.search(
        query="场站压力阈值",
        allowed_scopes={"internal"},
        roles={"tester"},
        min_score=0.0,
    )

    assert {item["chunk_id"] for item in as_alice} == {"alice-note", "shared-note"}
    assert {item["chunk_id"] for item in as_bob} == {"shared-note"}
    assert {item["chunk_id"] for item in anonymous} == {"shared-note"}


async def test_role_restricted_chunks_require_matching_role(session):
    await store.replace_all(
        session,
        "kb-roles",
        [
            _acl_record(
                chunk_id="ops-only",
                text="调压器检修作业票流程",
                allowed_roles=["ops"],
            ),
            _acl_record(chunk_id="all-staff", text="调压器检修期间的安全须知"),
        ],
    )
    await session.commit()

    retriever = await KnowledgeIndex().get(session, "kb-roles", force=True)
    assert retriever is not None

    with_ops, _ = retriever.search(
        query="调压器检修",
        allowed_scopes={"internal"},
        roles={"ops"},
        user_id="u1",
        min_score=0.0,
    )
    without_ops, _ = retriever.search(
        query="调压器检修",
        allowed_scopes={"internal"},
        roles={"tester"},
        user_id="u1",
        min_score=0.0,
    )

    assert "ops-only" in {item["chunk_id"] for item in with_ops}
    assert {item["chunk_id"] for item in without_ops} == {"all-staff"}


async def test_scope_filter_still_applies_to_acl_visible_chunks(session):
    """ACL 通过之后仍受 access_scope 限制：两层是叠加而不是替代。"""
    await store.replace_all(
        session,
        "kb-scope-acl",
        [
            _acl_record(
                chunk_id="conf-ops",
                text="机密压力阈值",
                scope="confidential",
                allowed_roles=["ops"],
            ),
        ],
    )
    await session.commit()

    retriever = await KnowledgeIndex().get(session, "kb-scope-acl", force=True)
    assert retriever is not None

    documents, _ = retriever.search(
        query="机密压力阈值",
        allowed_scopes={"public", "internal"},
        roles={"ops"},
        user_id="u1",
        min_score=0.0,
    )

    assert documents == []
