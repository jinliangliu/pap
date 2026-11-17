# tests/unit/test_agent_references.py
"""引用传递（P1）：四类 ref、三态拒绝、整单全有或全无。

这个模块守的不是「能把材料传过去」，而是**传不过去时说了什么**：

- 不可见必须与「读不到」分开。把无权读返回成空内容，B 会以为 A 那边本来
  就没有这份材料，然后编一个看起来合理的结论——错误静默传播，日志里一个字都没有；
- 一个引用不过就整单拒绝。部分成功会让 A 以为 B 看到了全部；
- 拒绝理由要点名缺的那个 scope。只说「引用不可用」，排障会停在「换份材料吧」，
  而真正该改的是授权。

对应 `docs/AGENT_CONNECT.md` §7.1 的第 3–8 条。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.artifact import Artifact
from app.models.audit import AuditEventType, AuditLog
from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import Document, DocumentStatus
from app.services import agent_service, reference_service
from app.tenancy.context import tenant_scope
from app.tools.builtins import delegate_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"
USER = "u-1"

# 别人的产物/会话。与 `USER` 只差一个字符串，但这一个字符串就是整条判定。
OTHER_USER = "u-2"


@pytest.fixture(autouse=True)
async def _clean(session, seeded_identity):
    for model in (Agent, Artifact, Document):
        await session.execute(delete(model))
    await session.commit()
    yield
    for model in (Agent, Artifact, Document):
        await session.execute(delete(model))
    await session.commit()


# ---------------------------------------------------------------- 辅助


def _ctx(**over) -> ToolExecutionContext:
    base = dict(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="t-1",
        conversation_id="conv-1",
        run_id="r-1",
        request_id="req-1",
        roles=("tester",),
        scopes=("tools:read", "knowledge.read", "knowledge.internal"),
        agent_name="a",
        agent_revision=1,
        agent_allowed_tools=None,
        agent_can_write=True,
    )
    base.update(over)
    return ToolExecutionContext(**base)


class _FakeGraph:
    """记录子调用收到了什么；默认给一个「有依据」的信封。"""

    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {
            "final_response": "已按材料核对。",
            "citations": [],
            "tool_observations": [{"tool_name": "search_knowledge", "success": True}],
            "missing_fields": [],
            "error_category": None,
        }
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, state, config):  # noqa: ANN001
        self.calls.append((state, config))
        return self.state


async def _seed_agent(name: str = "ops", **kwargs) -> str:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name=name,
                display_name=kwargs.pop("display_name", name),
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                **kwargs,
            )
            await db.commit()
            return str(row.id)


async def _make_artifact(
    *,
    user: str = USER,
    mime: str = "text/plain",
    data: bytes = b"hello",
    filename: str = "note.txt",
    conversation_id: str | None = "conv-1",
) -> str:
    """直接落一条产物 + 一份对象。走接口会多绕一圈，而这层要测的是解析。"""
    from app.storage.object_store import get_object_store

    artifact_id = uuid.uuid4()
    key = get_object_store().build_key(
        tenant_id=TENANT,
        user_id=user,
        artifact_id=artifact_id,
        suffix={"text/plain": "", "application/pdf": ".pdf", "text/markdown": ".md"}.get(
            mime, ""
        ),
    )
    await get_object_store().put(key, data)

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            db.add(
                Artifact(
                    id=artifact_id,
                    tenant_id=TENANT,
                    user_id=user,
                    # 默认与 `_ctx` 同一个会话；测「另一个会话」时传别的值。
                    conversation_id=conversation_id,
                    object_key=key,
                    filename=filename,
                    content_type=mime,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
            await db.commit()

    return str(artifact_id)


async def _make_document(
    *,
    acl: dict | None = None,
    status: DocumentStatus = DocumentStatus.READY,
    title: str = "应急规程",
) -> str:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = Document(
                tenant_id=TENANT,
                title=title,
                source_uri="upload://x",
                content_hash=uuid.uuid4().hex,
                status=status,
                metadata_json={"acl": acl} if acl else {},
            )
            db.add(row)
            await db.commit()
            return str(row.id)


async def _make_message(*, user: str = USER, text: str = "3 号泵昨天 14:20 跳停。") -> str:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            conversation = Conversation(
                tenant_id=TENANT,
                user_id=user,
                thread_id=f"th-{uuid.uuid4().hex[:8]}",
            )
            db.add(conversation)
            await db.flush()

            message = Message(
                tenant_id=TENANT,
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=text,
            )
            db.add(message)
            await db.commit()
            return str(message.id)


async def _audits(resource_id: str) -> list[AuditLog]:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog).where(
                    AuditLog.event_type == AuditEventType.AGENT_DELEGATED,
                    AuditLog.resource_id == resource_id,
                )
            )
            return list(rows.all())


# ---------------------------------------------------------------- 入参


def test_parse_refs_rejects_unknown_kind():
    with pytest.raises(reference_service.ReferenceInvalidError) as exc:
        reference_service.parse_refs([{"kind": "spreadsheet", "id": "x"}])
    # 报错要列出支持的类型，否则模型只会把同一个值再猜一遍。
    assert "artifact" in str(exc.value)


def test_parse_refs_rejects_empty_id():
    with pytest.raises(reference_service.ReferenceInvalidError):
        reference_service.parse_refs([{"kind": "entity", "id": "   "}])


def test_parse_refs_merges_duplicates():
    """A 看不出自己把同一份材料写了两遍，报错会让人以为引用机制坏了。"""
    specs = reference_service.parse_refs(
        [
            {"kind": "entity", "id": "T002509008"},
            {"kind": "entity", "id": "T002509008"},
            {"kind": "entity", "id": "T002509008", "section": "4.2"},
        ]
    )
    assert len(specs) == 2


def test_parse_refs_rejects_too_many():
    raw = [{"kind": "entity", "id": f"T{i}"} for i in range(reference_service.MAX_REFS + 1)]
    with pytest.raises(reference_service.ReferenceInvalidError):
        reference_service.parse_refs(raw)


def test_parse_refs_treats_missing_as_empty():
    assert reference_service.parse_refs(None) == ()
    assert reference_service.parse_refs([]) == ()


# ---------------------------------------------------------------- 产物三态


async def test_artifact_of_another_user_is_not_visible():
    """§7.1-3：别人的产物 → REF_NOT_VISIBLE，**不是空内容**。

    这条是整层的核心。返回空内容的话，B 会拿着「什么都没有」去作答，
    而 A 完全看不出材料没送到——错误被静默地传了一整圈。
    """
    artifact_id = await _make_artifact(user=OTHER_USER)

    with pytest.raises(reference_service.ReferenceNotVisibleError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )

    assert exc.value.code == "REF_NOT_VISIBLE"
    # 理由里不能带别人的文件名之外的任何内容——连标题都不给。
    assert exc.value.ref_id == artifact_id


async def test_deleted_artifact_is_gone():
    """§7.1-5：行删了就是 GONE，与「无权看」是两回事。

    两者混成一个错误，运维会去改授权；而真正要做的其实是重新上传。
    """
    artifact_id = await _make_artifact()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.get(Artifact, uuid.UUID(artifact_id))
            assert row is not None
            await db.delete(row)
            await db.commit()

    with pytest.raises(reference_service.ReferenceGoneError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )

    assert exc.value.code == "REF_GONE"


async def test_artifact_of_another_conversation_is_not_visible():
    """P3-c：产物绑了会话，就不能从别的会话引用。

    这条与「别人的产物」是两道不同的门：同一个人的东西，跨会话也不该串。
    少了它，「按会话保留」只剩一句写在文档里的话——产物照样能被任何
    拿到 id 的会话引用，而 id 是会出现在回答里的。
    """
    artifact_id = await _make_artifact(conversation_id="conv-other")

    with pytest.raises(reference_service.ReferenceNotVisibleError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )

    assert exc.value.code == "REF_NOT_VISIBLE"
    # 与「不属于当前用户」用的是同一个错误码，但理由必须能分清：
    # 一个是找错了人，一个是找错了会话，改的地方完全不同。
    assert "另一个会话" in str(exc.value)


async def test_unbound_artifact_stays_referenceable():
    """历史上传的通用材料（`conversation_id` 为空）不能被这条收紧误伤。

    收紧可见范围最容易顺手把存量一起废掉：那些附件上传时根本没绑会话。
    留这个口子的代价是「无主产物跨会话可见」，但它是用户自己传的材料，
    本来就该跟着人走而不是跟着某一场对话。
    """
    artifact_id = await _make_artifact(conversation_id=None)

    resolved = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
    )

    assert resolved[0].ref_id == artifact_id


async def test_conversation_check_is_skipped_without_a_conversation():
    """调用方没有会话时（如离线批处理），只按用户判定，不能一律拒绝。

    把「我没有会话」当成「会话不匹配」会让这类调用全部失败，而它们
    本来就无从比较——判据不足时应当放行到下一道门（同租户 + 同用户）。
    """
    artifact_id = await _make_artifact(conversation_id="conv-other")

    resolved = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=None,
    )

    assert resolved[0].ref_id == artifact_id


async def test_non_uuid_ref_id_is_gone_not_crash():
    """模型偶尔会把文件名当 id 传进来。这必须是可解释的失败，不是 500。"""
    with pytest.raises(reference_service.ReferenceGoneError):
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="artifact", ref_id="report.pdf"),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )


async def test_artifact_ref_copies_file_into_child_conversation(tmp_path, monkeypatch):
    """能读的产物要变成子调用会话里的一个附件句柄。

    为什么不把手上的对象键直接递过去：子调用的附件读取只认「本租户 + 本会话
    目录里的句柄」，递键过去，B 拿到的是一个读不了的值，而报错是「附件不存在」——
    比不给还难查（`object_store.py` 开头就写着「路径不是权限」）。
    """
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))

    # 一份最小的合法 PDF：`save_bytes` 会按文件头再核一次类型。
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    artifact_id = await _make_artifact(mime="application/pdf", data=pdf, filename="规程.pdf")

    refs = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
    )

    attachment = refs[0].attachment
    assert attachment is not None
    assert attachment["kind"] == "pdf"
    assert attachment["name"] == "规程.pdf"

    # 材料块里要有 file_id 与「怎么读」——只给句柄，模型不会知道该调哪个工具。
    block = reference_service.render_materials(refs)
    assert attachment["attachment_id"] in block
    assert "ocr_document" in block


async def test_markdown_artifact_is_handed_over_as_text_attachment(
    tmp_path, monkeypatch
):
    """Markdown 产物也能转交：读取工具直接返回原文，不依赖 OCR 服务。"""
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))

    md = "# 巡检规程\n\n1. 每日检查压力。\n".encode()
    artifact_id = await _make_artifact(
        mime="text/markdown", data=md, filename="规程.md"
    )

    refs = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
    )

    attachment = refs[0].attachment
    assert attachment is not None
    assert attachment["kind"] == "text"
    assert attachment["attachment_id"].startswith("txt_")
    assert attachment["attachment_id"] in reference_service.render_materials(refs)


async def test_unreadable_mime_degrades_to_metadata_only():
    """读取工具不认的产物只给元数据，且材料块里不能出现 file_id。

    给了 file_id 而读不了，模型会去调 OCR、失败、再解释一遍——
    比一开始就说「只有元数据」多烧两轮。
    `text/plain` 曾经是这里的例子，现在附件层与读取工具都支持它了；
    换成 Word 文档：有实体文件，但没有解析器，仍然只能给元数据。
    """
    artifact_id = await _make_artifact(mime="application/msword")

    refs = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="artifact", ref_id=artifact_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
    )

    assert refs[0].attachment is None
    block = reference_service.render_materials(refs)
    assert "file_id=" not in block
    assert "note.txt" in block


# ---------------------------------------------------------------- 知识文档


async def test_knowledge_above_scope_names_missing_scope():
    """§7.1-4：理由点名缺的 scope，不是「不可用」。

    只说「引用不可用」，排障会去换一份材料；而真正要改的是给 B 加授权。
    """
    document_id = await _make_document(
        acl={"access_scope": "confidential", "visibility": "tenant_shared"}
    )

    with pytest.raises(reference_service.ReferenceNotVisibleError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="knowledge", ref_id=document_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
            scopes=("tools:read", "knowledge.read", "knowledge.internal"),
        )

    assert exc.value.missing_scope == "knowledge.confidential"
    assert "confidential" in str(exc.value)
    assert exc.value.as_detail()["missing_scope"] == "knowledge.confidential"


async def test_knowledge_private_to_another_user_is_not_visible():
    document_id = await _make_document(
        acl={
            "access_scope": "internal",
            "visibility": "user_private",
            "owner_user_id": OTHER_USER,
        }
    )

    with pytest.raises(reference_service.ReferenceNotVisibleError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="knowledge", ref_id=document_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
            scopes=("knowledge.read", "knowledge.internal"),
        )

    assert "上传者本人" in str(exc.value)


async def test_knowledge_role_restriction_is_checked():
    document_id = await _make_document(
        acl={
            "access_scope": "internal",
            "visibility": "tenant_shared",
            "allowed_roles": ["运维"],
        }
    )

    with pytest.raises(reference_service.ReferenceNotVisibleError) as exc:
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="knowledge", ref_id=document_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
            scopes=("knowledge.read", "knowledge.internal"),
            roles=("tester",),
        )

    # 分级这一关过了，卡在角色上：理由必须说角色，不然会被当成授权不足去加 scope。
    assert "运维" in str(exc.value)


async def test_deleted_document_is_gone_not_invisible():
    """下架 ≠ 无权。指着已下架的文档说「权限不足」，会让人去翻授权配置。"""
    document_id = await _make_document(status=DocumentStatus.DELETED)

    with pytest.raises(reference_service.ReferenceGoneError):
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="knowledge", ref_id=document_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )


# ---------------------------------------------------------------- 会话消息


async def test_message_from_another_users_conversation_is_not_visible():
    message_id = await _make_message(user=OTHER_USER)

    with pytest.raises(reference_service.ReferenceNotVisibleError):
        await reference_service.resolve_refs(
            (reference_service.RefSpec(kind="message", ref_id=message_id),),
            tenant_id=TENANT,
            user_id=USER,
            conversation_id="conv-1",
        )


async def test_message_ref_carries_an_excerpt():
    message_id = await _make_message()

    refs = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="message", ref_id=message_id),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
    )

    assert "14:20" in refs[0].detail
    # 摘要，不是正文：材料块是「凭什么」，不是一份可再分发的内容副本。
    assert "user" in refs[0].detail


# ---------------------------------------------------------------- 实体


async def test_entity_ref_is_not_locally_judged():
    """站点授权在业务工具那一侧。这里再判一次会得到两套授权，早晚不一致。"""
    refs = await reference_service.resolve_refs(
        (reference_service.RefSpec(kind="entity", ref_id="T002509008"),),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id="conv-1",
        scopes=(),
    )

    assert refs[0].ref_id == "T002509008"
    assert "站点" in refs[0].detail


# ---------------------------------------------------------------- 委派闭环


async def test_one_bad_ref_rejects_the_whole_delegation(monkeypatch):
    """§7.1-6：5 个引用 1 个不可见 → 整单拒绝，子图一次都不许跑。

    部分成功最坏的地方不是少一份材料，而是 A 完全不知道少了：
    它会把 B 基于 4 份材料给出的结论当成 5 份的结论转述给用户。
    """
    await _seed_agent("ops")
    bad = await _make_artifact(user=OTHER_USER)

    fake = _FakeGraph()
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(),
        {
            "target": "ops",
            "task": "按这几份材料出一份结论",
            "refs": [
                {"kind": "entity", "id": "T1"},
                {"kind": "entity", "id": "T2"},
                {"kind": "entity", "id": "T3"},
                {"kind": "entity", "id": "T4"},
                {"kind": "artifact", "id": bad},
            ],
        },
    )

    assert result.success is False
    assert result.error_code == "REF_NOT_VISIBLE"
    assert fake.calls == [], "有一个引用不可用，子调用仍然跑了"

    # 被拒的这一次最值得查，所以「传了些什么」也要落账。
    audits = await _audits("ops")
    assert audits and audits[-1].metadata_json["refs"][-1]["id"] == bad


async def test_invalid_refs_are_rejected_before_any_lookup():
    await _seed_agent("ops")

    result = await delegate_tools.delegate_to_agent(
        _ctx(),
        {"target": "ops", "task": "看看", "refs": [{"kind": "spreadsheet", "id": "x"}]},
    )

    assert result.error_code == "DELEGATE_REF_INVALID"
    assert "artifact" in (result.message or "")


async def test_materials_reach_the_child_and_attachments_across(monkeypatch, tmp_path):
    """材料块写进子调用的消息，可读的产物变成子调用的附件。"""
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))
    await _seed_agent("ops")

    artifact_id = await _make_artifact(
        mime="application/pdf",
        data=b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n",
        filename="规程.pdf",
    )

    fake = _FakeGraph()
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(),
        {
            "target": "ops",
            "task": "按规程第 4.2 节判断这次跳停是否合规",
            "refs": [{"kind": "artifact", "id": artifact_id}],
        },
    )

    assert result.success is True
    child, _ = fake.calls[0]

    content = child["messages"][0].content
    assert isinstance(child["messages"][0], HumanMessage)
    assert "[材料]" in content
    assert "规程.pdf" in content
    # 附件默认不过桥，只有被显式引用的那一份进来。
    assert len(child["attachments"]) == 1
    assert child["attachments"][0]["kind"] == "pdf"


async def test_no_ref_means_no_attachments(monkeypatch):
    """不传 refs 时子调用一个附件都没有——「引用是显式动作」这条要能被证伪。"""
    await _seed_agent("ops")
    fake = _FakeGraph()
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    await delegate_tools.delegate_to_agent(_ctx(), {"target": "ops", "task": "看看"})

    child, _ = fake.calls[0]
    assert child["attachments"] == []
    assert "[材料]" not in child["messages"][0].content


async def test_blank_conclusion_without_evidence_is_not_bridged(monkeypatch):
    """§7.1-7：没有工具调用、没有声明缺什么、发起方也没给材料 → 不转述。

    这种回答只可能是凭空生成的。转述出去，等于把 A 的信用借给一段捏造。
    """
    await _seed_agent("ops")
    fake = _FakeGraph(
        {
            "final_response": "3 号泵昨天 14:20 跳停，是压力越限导致的。",
            "citations": [],
            "tool_observations": [],
            "missing_fields": [],
            "error_category": None,
        }
    )
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(), {"target": "ops", "task": "3 号泵昨天为什么跳停"}
    )

    assert result.success is False
    assert result.error_code == "DELEGATE_NO_EVIDENCE"
    assert "14:20" not in (result.message or "")

    audits = await _audits("ops")
    assert audits[-1].metadata_json["reject_reason"] == "no_evidence"


async def test_same_answer_passes_when_materials_were_given(monkeypatch):
    """同一条回答，只要发起方给过材料就放行。

    这条是上一条的反向约束：判据是「三样都没有」，不是「必须有 citations」。
    收紧成后者会把「基于 A 给的材料作答」整类正常转交一起拦掉。
    """
    await _seed_agent("ops")
    fake = _FakeGraph(
        {
            "final_response": "按材料看是压力越限。",
            "citations": [],
            "tool_observations": [],
            "missing_fields": [],
            "error_category": None,
        }
    )
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(),
        {
            "target": "ops",
            "task": "按材料判断",
            "refs": [{"kind": "entity", "id": "T002509008"}],
        },
    )

    assert result.success is True


async def test_ref_still_resolves_at_depth_two(monkeypatch):
    """§7.1-8：深度 2 时引用照样能解析。

    每跳都在**当前这一跳**的上下文里重判，所以「上一跳能看」不代表「这一跳能看」；
    但它也不该因为深度就衰减——那样第二跳会凭空少材料，而错误看起来像「B 没读懂」。
    """
    await _seed_agent("ops")
    document_id = await _make_document()

    fake = _FakeGraph()
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(delegation_path=("a", "b"), agent_name="b"),
        {
            "target": "ops",
            "task": "按这份规程判断",
            "refs": [{"kind": "knowledge", "id": document_id}],
        },
    )

    assert result.success is True
    assert "应急规程" in fake.calls[0][0]["messages"][0].content


async def test_audit_records_ref_ids_without_titles_or_excerpts(monkeypatch):
    """§7.1-2：审计里的 refs 只有 kind 与 id。

    与 `task_sha256` 同一条理由：标题和摘录里可能带业务细节，而审计是长期账。
    """
    await _seed_agent("ops")
    document_id = await _make_document(title="3 号泵跳停分析（2026-11）")

    fake = _FakeGraph()
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    await delegate_tools.delegate_to_agent(
        _ctx(),
        {
            "target": "ops",
            "task": "按这份材料判断",
            "refs": [{"kind": "knowledge", "id": document_id}],
        },
    )

    metadata = (await _audits("ops"))[-1].metadata_json
    assert metadata["refs"] == [{"kind": "knowledge", "id": document_id}]
    assert "3 号泵" not in str(metadata["refs"])


async def test_audit_is_written_even_when_refs_are_rejected(monkeypatch):
    """解析失败的那一次最值得查：要能看出「当时传的是哪几个 id」。"""
    await _seed_agent("ops")
    bad = await _make_artifact(user=OTHER_USER)

    await delegate_tools.delegate_to_agent(
        _ctx(),
        {"target": "ops", "task": "看看", "refs": [{"kind": "artifact", "id": bad}]},
    )

    metadata = (await _audits("ops"))[-1].metadata_json
    assert metadata["status"] == "rejected"
    assert metadata["refs"] == [{"kind": "artifact", "id": bad}]
    assert metadata["reject_reason"] == "ref:REF_NOT_VISIBLE"


def test_render_materials_is_empty_without_refs():
    assert reference_service.render_materials(()) == ""


def test_render_materials_tells_the_model_what_is_missing():
    """最后那句必须在：模型天然会把「拿到的材料」当成「全部背景」。"""
    block = reference_service.render_materials(
        (
            reference_service.ResolvedRef(
                kind="entity", ref_id="T1", detail="（业务对象）T1"
            ),
        )
    )
    assert "不是整个对话" in block
    assert "未列出的内容不要假设其存在" in block
