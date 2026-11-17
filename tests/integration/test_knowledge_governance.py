# tests/integration/test_knowledge_governance.py
"""知识治理闭环：加工流水线可视化 → 反馈 → 评测 → 生命周期。

这一组钉的是「界面上能看到的每个数字都来自真实写入」：

- 上传后 ``/governance`` 的切片数、快照覆盖、标签必须对得上；
- 同一条回答重复点赞只留一行（否则差评清单会被同一个人的反复点击灌满）；
- 没标注期望文档的用例必须计入 ``unscored`` 而不是把指标拉低；
- 同一标准的两个版本并存时，旧版要被抓出来——这是唯一会静默伤害检索质量的判据。
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
from app.models.knowledge_gov import (
    KnowledgeEvalCase,
    KnowledgeEvalRun,
    KnowledgeFeedback,
)
from app.models.rbac import Role, RolePermission, User, UserRole
from app.tenancy.context import tenant_scope

TENANT = "default"
BASE = "/api/v1/knowledge"
WRITER_SUBJECT = "gov-writer"

BODY_2020 = """# 城镇燃气调压器

## 4 技术要求

调压器的关闭压力等级应与进口压力相匹配，出口压力波动不超过设定值的 10%。

## 5 试验方法

关闭压力试验应在额定流量下连续进行三次，取最大值。
"""

BODY_2011 = """# 城镇燃气调压器

## 4 技术要求

调压器出口压力应在设定值附近保持稳定。

## 5 试验方法

关闭压力试验按原规程执行。
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
    role = await session.scalar(select(Role).where(Role.name == "gov_writer"))

    if role is None:
        role = Role(name="gov_writer", description="知识治理")
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
        user = User(external_subject=WRITER_SUBJECT, display_name=WRITER_SUBJECT, enabled=True)
        session.add(user)
        await session.flush()

    exists = await session.scalar(
        select(UserRole).where(UserRole.tenant_id == TENANT, UserRole.user_id == user.id)
    )

    if exists is None:
        session.add(UserRole(tenant_id=TENANT, user_id=user.id, role_id=role.id))

    await session.flush()


async def _clear(session) -> None:
    with tenant_scope(TENANT):
        for model in (
            KnowledgeFeedback,
            KnowledgeEvalCase,
            KnowledgeEvalRun,
            DocumentChunk,
            Document,
            KnowledgeIndexState,
        ):
            await session.execute(delete(model))


@pytest.fixture
async def kb(session, seeded_identity):
    await _clear(session)
    await _ensure_writer(session)
    await session.commit()

    upload_dir = Path(settings.knowledge_upload_dir)
    shutil.rmtree(upload_dir, ignore_errors=True)
    get_knowledge_index().invalidate()

    yield

    get_knowledge_index().invalidate()
    await _clear(session)
    await session.commit()
    shutil.rmtree(upload_dir, ignore_errors=True)


async def _upload(
    http: AsyncClient,
    *,
    title: str,
    body: str,
    name: str = "doc.md",
) -> dict:
    response = await http.post(
        f"{BASE}/documents",
        files={"file": (name, body.encode("utf-8"), "text/markdown")},
        data={"title": title, "access_scope": "internal"},
    )
    assert response.status_code == 201, response.text

    return response.json()


async def test_governance_overview_reports_every_stage(kb):
    async with _client() as http:
        item = await _upload(
            http,
            title="GB 27791-2020 城镇燃气调压器",
            body=BODY_2020,
        )

        overview = (await http.get(f"{BASE}/governance")).json()
        stages = {stage["id"]: stage for stage in overview["stages"]}

        assert set(stages) == {
            "parse",
            "clean",
            "structure",
            "chunk",
            "metadata",
            "vector",
            "index",
            "graph",
        }

        assert stages["parse"]["status"] == "active"
        assert stages["chunk"]["metrics"][0]["value"] == item["chunk_count"]
        # 未实现的两个阶段必须是 not_enabled 且带原因，不能是 0% 的进度条。
        assert stages["vector"]["status"] == "not_enabled"
        assert stages["graph"]["status"] == "not_enabled"
        assert stages["vector"]["reason"] and stages["vector"]["trigger"]

        assert overview["totals"]["chunks"] == item["chunk_count"]
        assert overview["coverage"]["with_snapshot"] == 1
        assert overview["coverage"]["missing"] == 0
        # 打标结果进了标准清单：页面要能按标准号看覆盖。
        assert "GB 27791-2020" in overview["totals"]["standards"]


async def test_document_pipeline_detail_is_not_derived_after_upload(kb):
    async with _client() as http:
        item = await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)
        detail = (await http.get(f"{BASE}/documents/{item['id']}/pipeline")).json()

        assert detail["derived"] is False
        assert detail["snapshot"]["parse"]["extractor"] == "md"
        assert detail["snapshot"]["structure"]["sections"] >= 1
        assert detail["snapshot"]["chunk"]["chunks"] == item["chunk_count"]
        assert detail["tags"]["standard"] == "GB 27791-2020"
        assert detail["tags"]["year"] == 2020


async def test_feedback_is_idempotent_per_message(kb):
    payload = {
        "verdict": "up",
        "message_id": "msg-1",
        "conversation_id": "conv-1",
        "query": "关闭压力试验怎么做",
        "answer": "应在额定流量下连续进行三次。",
    }

    async with _client() as http:
        first = (await http.post(f"{BASE}/feedback", json=payload)).json()
        assert first["verdict"] == "up"

        # 同一人同一消息改判：覆盖而不是新增。
        changed = (
            await http.post(
                f"{BASE}/feedback",
                json={**payload, "verdict": "down", "reason": "outdated", "comment": "条文已作废"},
            )
        ).json()

        assert changed["id"] == first["id"]
        assert changed["verdict"] == "down"
        assert changed["reason"] == "outdated"

        listing = (await http.get(f"{BASE}/feedback")).json()
        assert listing["total"] == 1

        # 赞的时候不留原因：残留一个上次的「内容过期」会让统计全错。
        back_to_up = (await http.post(f"{BASE}/feedback", json=payload)).json()
        assert back_to_up["reason"] is None


async def test_feedback_rejects_unknown_reason(kb):
    async with _client() as http:
        res = await http.post(
            f"{BASE}/feedback",
            json={"verdict": "down", "reason": "我不喜欢", "query": "q"},
        )

        assert res.status_code == 400
        assert "reason" in res.json()["detail"]


async def test_member_can_flag_answer_but_not_read_governance(kb):
    """点赞/踩是使用者的动作，不挂 knowledge:write；治理页才要权限。"""
    async with _client(subject="test-user") as http:
        posted = await http.post(
            f"{BASE}/feedback",
            json={"verdict": "down", "reason": "not_found_but_had", "query": "报警等级怎么分"},
        )
        assert posted.status_code == 200

        assert (await http.get(f"{BASE}/governance")).status_code == 403
        assert (await http.get(f"{BASE}/feedback")).status_code == 403
        assert (await http.get(f"{BASE}/eval/cases")).status_code == 403


async def test_bad_case_becomes_eval_case_and_scores(kb):
    async with _client() as http:
        item = await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)

        feedback = (
            await http.post(
                f"{BASE}/feedback",
                json={
                    "verdict": "down",
                    "message_id": "msg-9",
                    "reason": "not_relevant",
                    "query": "关闭压力试验应连续进行几次",
                    "answer": "没有找到依据。",
                },
            )
        ).json()

        case = (
            await http.post(
                f"{BASE}/feedback/{feedback['id']}/case",
                json={"expect_document": [item["logical_id"]]},
            )
        ).json()

        assert case["source"] == "feedback"
        assert case["scored"] is True

        cases = (await http.get(f"{BASE}/eval/cases")).json()
        assert cases["total"] == 1
        assert cases["scored"] == 1

        run = (await http.post(f"{BASE}/eval/run", json={"k": 3})).json()

        assert run["metrics"]["scored"] == 1
        assert run["metrics"]["unscored"] == 0
        # 问题与切片有词面交集，BM25 必须能捞到；捞不到说明评测链路接错了。
        assert run["metrics"]["recall"] == 1.0
        assert run["index_version"].startswith("v")

        history = (await http.get(f"{BASE}/eval/runs")).json()
        assert history["total"] == 1

        detail = (await http.get(f"{BASE}/eval/runs/{run['id']}")).json()
        assert detail["detail"][0]["hit"] is True

        # 同一条反馈重复转：不能变成两条重复用例。
        again = (
            await http.post(
                f"{BASE}/feedback/{feedback['id']}/case",
                json={"expect_document": [item["logical_id"]]},
            )
        ).json()
        assert again["id"] == case["id"]
        assert (await http.get(f"{BASE}/eval/cases")).json()["total"] == 1


async def test_unlabelled_case_is_reported_not_counted(kb):
    async with _client() as http:
        await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)

        created = await http.post(
            f"{BASE}/eval/cases",
            json={"query": "关闭压力试验要做几次", "expect_document": []},
        )
        assert created.status_code == 200
        assert created.json()["scored"] is False

        run = (await http.post(f"{BASE}/eval/run", json={"k": 3})).json()

        assert run["metrics"]["cases"] == 1
        assert run["metrics"]["scored"] == 0
        assert run["metrics"]["unscored"] == 1
        # 没有计分用例时不能报出一个假的高分。
        assert run["metrics"]["recall"] == 0.0


async def test_case_validation_rejects_empty_document_id(kb):
    async with _client() as http:
        res = await http.post(
            f"{BASE}/eval/cases",
            json={"query": "q", "expect_document": [""]},
        )

        assert res.status_code == 400
        assert "空值" in res.json()["detail"]


async def test_eval_run_without_cases_is_a_client_error(kb):
    async with _client() as http:
        await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)

        res = await http.post(f"{BASE}/eval/run", json={"k": 3})

        assert res.status_code == 400
        assert "用例" in res.json()["detail"]


async def test_reindex_backfills_snapshot_for_legacy_rows(kb, session):
    """老行没有流水线快照时，「重建」必须真的重建。

    快照是后加的字段。若增量判定只看「内容哈希 + 切片数 + 切片器版本」，
    老文档点重建会被判 unchanged、什么都不写，治理页永远显示「未采集」。
    """
    import uuid

    async with _client() as http:
        item = await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)

        # 模拟改造前入库的行：抹掉 pipeline 节。
        with tenant_scope(TENANT):
            row = await session.get(Document, uuid.UUID(item["id"]))
            meta = dict(row.metadata_json or {})
            meta.pop("pipeline", None)
            row.metadata_json = meta
        await session.commit()

        missing = (await http.get(f"{BASE}/documents/{item['id']}/pipeline")).json()
        assert missing["derived"] is True

        reindexed = await http.post(f"{BASE}/documents/{item['id']}/reindex")
        assert reindexed.status_code == 200, reindexed.text

    # 写请求走的是另一条 session，本 session 的 identity map 里还是旧对象。
    session.expire_all()

    with tenant_scope(TENANT):
        row = await session.get(Document, uuid.UUID(item["id"]))
        snapshot = (row.metadata_json or {}).get("pipeline") or {}

    assert snapshot.get("chunk", {}).get("chunks") == item["chunk_count"]
    assert snapshot.get("metadata", {}).get("standard") == "GB 27791-2020"


async def test_reindex_repairs_title_carrying_ocr_tail(kb, session):
    """库里的脏标题要能靠「重建」修掉。

    标题归一规则改之前，上传兜底直接拿文件名当标题，线上因此留着一份
    `…技术规范.pdf_by_PaddleOCR-VL-1.6`。只把清洗加进入库是不够的：
    老行点重建时内容哈希、片数、切片器版本都没变，被判 unchanged 直接返回，
    标题照旧——修完还得靠手工改库。所以标题也是「这份文档是否已入库」的一部分。
    """
    import uuid

    dirty = "GB 27791-2020 城镇燃气调压器.pdf_by_PaddleOCR-VL-1.6"

    async with _client() as http:
        item = await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)

        # 模拟改造前入库的行：标题带着 OCR 长尾。
        with tenant_scope(TENANT):
            row = await session.get(Document, uuid.UUID(item["id"]))
            row.title = dirty
        await session.commit()

        reindexed = await http.post(f"{BASE}/documents/{item['id']}/reindex")
        assert reindexed.status_code == 200, reindexed.text

    session.expire_all()

    with tenant_scope(TENANT):
        row = await session.get(Document, uuid.UUID(item["id"]))

    assert row.title == "GB 27791-2020 城镇燃气调压器"


async def test_second_sync_of_same_content_stays_unchanged(kb, session):
    """补齐快照之后，恢复原来的增量语义：内容没变就不再重建。"""
    import uuid

    async with _client() as http:
        item = await _upload(http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020)
        await http.post(f"{BASE}/documents/{item['id']}/reindex")

    with tenant_scope(TENANT):
        row = await session.get(Document, uuid.UUID(item["id"]))
        before = dict(row.metadata_json or {})

    async with _client() as http:
        await http.post(f"{BASE}/documents/{item['id']}/reindex")

    with tenant_scope(TENANT):
        row = await session.get(Document, uuid.UUID(item["id"]))
        after = dict(row.metadata_json or {})

    # 快照被重写但内容一样：updated_at 这类随重建变化的字段不参与比较。
    assert after.get("document_id") == before.get("document_id")
    assert (after.get("pipeline") or {}).get("chunk") == (before.get("pipeline") or {}).get("chunk")


async def test_lifecycle_flags_superseded_version(kb):
    async with _client() as http:
        old = await _upload(
            http,
            title="GB 27791-2011 城镇燃气调压器",
            body=BODY_2011,
            name="old.md",
        )
        new = await _upload(
            http,
            title="GB 27791-2020 城镇燃气调压器",
            body=BODY_2020,
            name="new.md",
        )

        report = (await http.get(f"{BASE}/lifecycle")).json()
        by_id = {item["id"]: item for item in report["items"]}

        assert old["id"] in by_id
        assert by_id[old["id"]]["suggest"] == "retire"
        assert by_id[old["id"]]["severity"] == "high"
        assert any(reason["code"] == "superseded" for reason in by_id[old["id"]]["reasons"])

        # 新版可以因为「版本偏老」出现在清单里，但绝不能被判成「被替代」：
        # 那会让页面建议下架唯一正确的那一份。
        newer_reasons = {reason["code"] for reason in by_id.get(new["id"], {}).get("reasons", [])}
        assert "superseded" not in newer_reasons
        assert by_id.get(new["id"], {}).get("suggest", "review") != "retire"

        assert report["summary"]["superseded"] == 1
        # 只给候选，不自动删：这是硬边界。
        assert report["policy"]["auto_retire"] is False
        assert report["incremental"]["index_version"].startswith("v")


async def test_lifecycle_retire_needs_explicit_ids(kb):
    async with _client() as http:
        old = await _upload(
            http, title="GB 27791-2011 城镇燃气调压器", body=BODY_2011, name="old.md"
        )
        await _upload(
            http, title="GB 27791-2020 城镇燃气调压器", body=BODY_2020, name="new.md"
        )

        empty = await http.post(f"{BASE}/lifecycle/retire", json={"document_ids": []})
        assert empty.status_code == 400

        result = (await http.post(f"{BASE}/lifecycle/retire", json={"document_ids": [old["id"]]})).json()

        assert result["retired"] == [old["id"]]
        assert "已下架 1 份" in result["message"]

        remaining = (await http.get(f"{BASE}/documents")).json()
        assert remaining["total"] == 1
        assert remaining["items"][0]["title"] == "GB 27791-2020 城镇燃气调压器"
