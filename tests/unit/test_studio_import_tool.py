# tests/unit/test_studio_import_tool.py
"""构建助手的 studio_import：会话里的附件 → 知识库 / 技能 / 工具。

它替代的是原来的侧栏「从文件新建」：判类依据从「人在弹层里选」变成
「文件内容 + 用户这句话说的用途」。因此这里守四条：

1. **判类看内容，不看是谁点进来的**：带 front matter 的 md 是技能，
   其余文档进知识库；用户说了目标时以用户为准，并在结果里说明原本判成什么；
2. **文件名从消息元数据里找回来**：磁盘上只按句柄存（`txt_xxx.txt`），
   拿落盘名判类会把 OpenAPI 认成知识库文档——错在「已经入库」之后；
3. **图片不进来**：没有「原文切片」这回事，只能 OCR；
4. **权限按落点判**：知识库 / 技能 / 工具三套 scope，preflight 先拒一次，
   别让人对着一张注定失败的审批卡点确认。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.conversation import Conversation, Message, MessageRole
from app.models.custom_tool import CustomTool
from app.models.document import Document, DocumentChunk, KnowledgeIndexState
from app.models.skill import Skill
from app.services import attachment_store
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"
CONVERSATION = "3f2b8c1e-4a5d-4c6b-9e70-1d2c3b4a5f60"

SKILL_MD = """---
name: valve-patrol
display_name: 阀门巡检
description: 按阀位反馈与压差判断是否需要复检
trigger_keywords: [巡检, 阀位]
---

# 阀门巡检

1. 读阀位反馈
2. 比对上下游压差
"""

PLAIN_MD = """# 调压箱操作规程

1. 检查进出口阀门
2. 记录进出口压力
"""

# PaddleOCR 直接吐的 md：表格仍是 HTML，标签会占掉大半字符。
TABLE_MD = """# 5 要求

<table border=1 style='word-wrap: break-word;'>
<tr><td>项目</td><td>限值</td></tr>
<tr><td>电压</td><td>1 000 V</td></tr>
</table>

正文一段。
"""

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "站点服务", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/stations/{station_id}": {
            "get": {
                "operationId": "studioProbeStation",
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
        }
    },
}


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "studio-thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "roles": ("dev",),
        "scopes": (
            "tools:read",
            "knowledge.read",
            "knowledge.internal",
            "knowledge:write",
            "skill:write",
        ),
        "conversation_id": CONVERSATION,
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean(session, seeded_identity, tmp_path, monkeypatch):
    # 附件根目录必须落在临时目录：默认 ./data/uploads 会写进仓库。
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path / "attachments"))

    with tenant_scope(TENANT):
        for model in (Skill, DocumentChunk, Document, KnowledgeIndexState):
            await session.execute(delete(model))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(CustomTool))
    await session.commit()

    yield

    with tenant_scope(TENANT):
        for model in (Skill, DocumentChunk, Document, KnowledgeIndexState):
            await session.execute(delete(model))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(CustomTool))
    await session.commit()


def _store(body: str | bytes, *, name: str, mime: str = "text/markdown") -> str:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    record = attachment_store.save_bytes(
        tenant_id=TENANT,
        conversation_id=CONVERSATION,
        raw=raw,
        mime=mime,
        name=name,
    )
    return record.attachment_id


async def _import(arguments: dict) -> object:
    return await studio_admin_tools.studio_import(_ctx(), arguments)


async def _documents(session) -> list[Document]:
    with tenant_scope(TENANT):
        return list((await session.scalars(select(Document))).all())


async def _skills(session) -> list[Skill]:
    with tenant_scope(TENANT):
        return list((await session.scalars(select(Skill))).all())


async def _chunks(session) -> list[DocumentChunk]:
    with tenant_scope(TENANT):
        return list((await session.scalars(select(DocumentChunk))).all())


# ---------------- 判类 ----------------


async def test_plain_markdown_goes_to_knowledge(session):
    file_id = _store(PLAIN_MD, name="规程.md")

    result = await _import({"file_id": file_id, "filename": "规程.md"})

    assert result.success is True
    assert result.result["target"] == "knowledge"
    assert "已导入知识库" in result.message

    docs = await _documents(session)
    assert len(docs) == 1
    assert docs[0].title == "规程"

    with tenant_scope(TENANT):
        chunks = list((await session.scalars(select(DocumentChunk))).all())
    assert len(chunks) >= 1


async def test_front_matter_markdown_becomes_skill(session):
    file_id = _store(SKILL_MD, name="valve.md")

    result = await _import({"file_id": file_id, "filename": "valve.md"})

    assert result.success is True
    assert result.result["target"] == "skill"

    skills = await _skills(session)
    assert [row.name for row in skills] == ["valve-patrol"]


async def test_openapi_json_becomes_tool_for_admin(session):
    """`.json` 落盘是 `.txt`，靠原始文件名才判得对——这条用例盯的就是这一步。"""
    file_id = _store(json.dumps(OPENAPI), name="api.json", mime="text/plain")

    result = await studio_admin_tools.studio_import(
        _ctx(scopes=("tools:read",), is_platform_admin=True),
        {"file_id": file_id, "filename": "api.json"},
    )

    assert result.success is True
    assert result.result["target"] == "tool"

    with tenant_scope(TENANT):
        names = set((await session.scalars(select(CustomTool.name))).all())
    assert "studioprobestation" in names


async def test_explicit_target_overrides_detection(session):
    """判错了要能改：同一份带 front matter 的文件强制进知识库。"""
    file_id = _store(SKILL_MD, name="valve.md")

    result = await _import(
        {"file_id": file_id, "filename": "valve.md", "target": "knowledge"}
    )

    assert result.success is True
    assert result.result["detected_target"] == "skill"
    assert "原本判成「技能」" in result.message
    assert len(await _documents(session)) == 1
    assert await _skills(session) == []


async def test_ocr_html_table_is_cleaned_and_reported(session):
    """PaddleOCR 的原始 md 要先洗掉表格标签再入库，而且必须说出来。

    「入库 287 片」与「你传了 56 万字」摆在一起时，人第一反应是「后半本没进去」。
    所以结果里要有 cleanup（原文多少、洗后多少、几张表），消息里要把整条链路
    说出来——清洗压掉多少、切成几节几片、打了哪些标，而不是只回一个片数。
    """
    file_id = _store(TABLE_MD, name="std.md")

    result = await _import(
        {"file_id": file_id, "filename": "std.md", "target": "knowledge"}
    )

    assert result.success is True

    cleanup = result.result["result"]["cleanup"]
    assert cleanup["tables"] == 1
    assert cleanup["chars_after"] < cleanup["chars_before"]

    docs = await _documents(session)
    assert len(docs) == 1

    chunks = await _chunks(session)
    assert chunks, "清洗后仍然要切成片"
    assert all("<td" not in chunk.content for chunk in chunks)

    assert "已转成 Markdown 表格入库" not in result.message
    # 有快照时改说完整链路：清洗这一步要带上「几张表、压掉多少字」，
    # 与旧的单句说的是同一件事，只是不再单列一句。
    assert "加工链路" in result.message
    assert "清洗 1 张表" in result.message
    assert f"{cleanup['chars_after']:,} 字" in result.message
    assert "切片" in result.message


async def test_import_reports_metadata_tags(session):
    """打标结果要回到对话里，否则「有没有认出这是哪份标准」只能去治理页看。

    这条守的是「标签行渲染」而不是抽词质量：抽词质量由 tagging 自己的单测管。
    """
    file_id = _store(
        "# 5 要求\n\n调压箱进出口压力测试。",
        name="GB 27791-2020 城镇燃气调压箱.md",
    )

    result = await _import(
        {
            "file_id": file_id,
            "filename": "GB 27791-2020 城镇燃气调压箱.md",
            "target": "knowledge",
        }
    )

    assert result.success is True
    assert result.result["result"]["tags"]
    assert "标签：" in result.message
    assert "GB 27791-2020" in result.message
    assert "打标" in result.message


# ---------------- 文件名 ----------------

async def test_filename_is_read_back_from_message_metadata(session):
    """磁盘名是句柄，判类必须回到消息元数据里的原始文件名。"""
    file_id = _store(SKILL_MD, name="valve.md", mime="text/markdown")

    conversation_id = uuid.UUID(CONVERSATION)
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            db.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=TENANT,
                    user_id="actor-1",
                    thread_id="studio-thread-1",
                    channel="console",
                )
            )
            db.add(
                Message(
                    tenant_id=TENANT,
                    conversation_id=conversation_id,
                    role=MessageRole.USER,
                    content="把这份做成技能",
                    metadata_json={
                        "attachments": [
                            {
                                "attachment_id": file_id,
                                "kind": "text",
                                "mime": "text/markdown",
                                "size": len(SKILL_MD.encode()),
                                "name": "valve.md",
                            }
                        ]
                    },
                )
            )
            await db.commit()

    # 不传 filename：只能从元数据里找。
    result = await _import({"file_id": file_id})

    assert result.success is True
    assert result.result["target"] == "skill"
    assert [row.name for row in await _skills(session)] == ["valve-patrol"]


async def test_missing_filename_falls_back_and_says_so(session):
    """两处都拿不到名字时按 MIME 兜底，并明确提示判类可能不准。"""
    file_id = _store(PLAIN_MD, name="规程.md", mime="text/plain")

    result = await _import({"file_id": file_id})

    assert result.success is True
    assert "兜底" in result.message
    assert len(await _documents(session)) == 1


# ---------------- 拒绝 ----------------


async def test_image_attachment_is_refused():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    file_id = _store(png, name="chart.png", mime="image/png")

    result = await _import({"file_id": file_id})

    assert result.success is False
    assert "图片不能" in result.message


async def test_unknown_file_id_is_rejected():
    result = await _import({"file_id": "txt_0123456789ab"})

    assert result.success is False
    assert "附件不存在" in result.message


async def test_unknown_target_is_rejected():
    file_id = _store(PLAIN_MD, name="规程.md")

    result = await _import({"file_id": file_id, "target": "spreadsheet"})

    assert result.success is False
    assert "未知的导入目标" in result.message


# ---------------- 权限 ----------------


async def test_preflight_requires_one_import_scope():
    denied = await studio_admin_tools._preflight_import(
        _ctx(scopes=("tools:read",)), {"file_id": "txt_0123456789ab"}
    )
    allowed = await studio_admin_tools._preflight_import(
        _ctx(scopes=("skill:write",)), {"file_id": "txt_0123456789ab"}
    )

    assert denied and "knowledge:write" in denied
    assert allowed is None


async def test_preflight_checks_the_stated_target():
    knowledge_only = _ctx(scopes=("knowledge:write",))

    assert (
        await studio_admin_tools._preflight_import(
            _ctx(scopes=("skill:write",)), {"target": "knowledge"}
        )
    ) is not None
    assert (
        await studio_admin_tools._preflight_import(
            knowledge_only, {"target": "knowledge"}
        )
    ) is None
    assert (
        await studio_admin_tools._preflight_import(knowledge_only, {"target": "tool"})
    ) == "导入工具接口是平台级操作，需要平台管理员权限。"


async def test_reader_is_refused_before_writing(session):
    file_id = _store(PLAIN_MD, name="规程.md")

    result = await studio_admin_tools.studio_import(
        _ctx(scopes=("tools:read",)), {"file_id": file_id, "filename": "规程.md"}
    )

    assert result.success is False
    assert "knowledge:write" in result.message
    assert await _documents(session) == []
