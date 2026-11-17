# tests/unit/test_studio_readback_verify.py
"""入库与下架之后的回读核验。

审批卡上的 VERIFIED 是给人看的结论，不能靠「工具自己说成功了」：
`ingest_upload` 里那次读回与写入在同一个事务里，即使提交失败也照样读得到。

入库核三条——文档在、状态 ready、切片不为空；
下架核两条——状态是 deleted、活跃切片归零（只看状态会漏掉「标了下架、
检索仍命中」那种失败）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.tenancy.context import tenant_scope
from app.tools.builtins.studio_admin_tools import (
    verify_document_retired,
    verify_ingested_document,
)
from app.tools.types import ToolExecutionContext

TENANT = "default"


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "conversation_id": "conv-1",
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean_db():
    async def _purge() -> None:
        async with async_session_factory() as db:
            with tenant_scope(TENANT):
                await db.execute(delete(DocumentChunk).where(DocumentChunk.tenant_id == TENANT))
                await db.execute(delete(Document).where(Document.tenant_id == TENANT))
            await db.commit()

    await _purge()
    yield
    await _purge()


async def _make_document(*, chunks: int = 2, status: DocumentStatus = DocumentStatus.READY) -> str:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = Document(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                title="GB/T 41315-2022 城镇燃气输配系统用安全切断阀",
                source_uri="uploads/a.md",
                source_type="file",
                content_hash=uuid.uuid4().hex,
                status=status,
                metadata_json={"acl": {"access_scope": "internal"}},
            )
            db.add(row)
            await db.flush()

            for index in range(chunks):
                db.add(
                    DocumentChunk(
                        tenant_id=TENANT,
                        document_id=row.id,
                        chunk_index=index,
                        content=f"第 {index} 片",
                    )
                )

            await db.commit()
            return str(row.id)


async def test_reads_the_document_back_in_a_fresh_session():
    document_id = await _make_document()

    result = await verify_ingested_document(
        _ctx(external_operation_id=document_id), {"file_id": "txt_0123456789ab"}
    )

    assert result.verified is True
    assert result.result["chunk_count"] == 2
    assert result.verification["source"] == "documents_table"
    assert result.verification["ref"] == document_id


async def test_missing_document_is_not_verified():
    """写完了但查不到，就是没入库。别把接口返回的 success 当成结论。"""
    result = await verify_ingested_document(_ctx(external_operation_id=str(uuid.uuid4())), {})

    assert result.verified is False
    assert result.error_code == "VERIFY_DOCUMENT_MISSING"


async def test_deleted_document_is_not_verified():
    document_id = await _make_document(status=DocumentStatus.DELETED)

    result = await verify_ingested_document(_ctx(external_operation_id=document_id), {})

    assert result.verified is False
    assert result.error_code == "VERIFY_DOCUMENT_NOT_READY"


async def test_document_without_chunks_is_not_verified():
    """「文档在库里、一片都没切」在列表上看着完全正常，检索时什么都不出。"""
    document_id = await _make_document(chunks=0)

    result = await verify_ingested_document(_ctx(external_operation_id=document_id), {})

    assert result.verified is False
    assert result.error_code == "VERIFY_DOCUMENT_EMPTY"


async def test_skill_and_tool_imports_have_no_readback_target():
    """技能与工具不落 documents 表，没有可回读的落点就如实说未核验。"""
    result = await verify_ingested_document(_ctx(), {"target": "skill"})

    assert result.verified is False
    assert result.verification["reason"] == "no_document"


# ---------------- 下架 ----------------


async def test_retired_document_reads_back_with_no_live_chunks():
    document_id = await _make_document(chunks=0, status=DocumentStatus.DELETED)

    result = await verify_document_retired(_ctx(external_operation_id=document_id), {})

    assert result.verified is True
    assert result.verification["chunks"] == 0


async def test_still_ready_document_is_not_retired():
    document_id = await _make_document()

    result = await verify_document_retired(_ctx(external_operation_id=document_id), {})

    assert result.verified is False
    assert result.error_code == "VERIFY_DOCUMENT_NOT_RETIRED"


async def test_retired_marker_with_live_chunks_is_not_verified():
    """标了下架、切片还活着：列表里没了、检索照样命中。

    这是下架这条路上最难发现的一种失败，只看 status 会判成「已下架」。
    """
    document_id = await _make_document(chunks=3, status=DocumentStatus.DELETED)

    result = await verify_document_retired(_ctx(external_operation_id=document_id), {})

    assert result.verified is False
    assert result.error_code == "VERIFY_CHUNKS_STILL_ACTIVE"
    assert result.verification["chunks"] == 3


async def test_retire_falls_back_to_the_argument_id():
    """回读上下文里没有操作号时，用工具参数里的 document_id。

    老审批单（这次改动之前创建的）没有 `external_operation_id`，不该因此
    全部判成「无法核验」。
    """
    document_id = await _make_document(chunks=0, status=DocumentStatus.DELETED)

    result = await verify_document_retired(_ctx(), {"document_id": document_id})

    assert result.verified is True
