# tests/integration/test_conversation_purge.py
"""删除会话必须销毁全部痕迹，而不是只把列表里的那一行去掉。

一次对话至少落在四处：conversations/messages、agent_runs 的输入输出摘要、
LangGraph 图状态（含 OCR 正文）、磁盘上的原始文件与会话产物。
只删第一处时前端看起来干净了，其余三处仍能把对话原样还原——线上曾积累
49 个线程的 checkpoint 与 18 条失去归属的运行记录。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agents.planner import reset_planner, set_planner_for_tests
from app.agents.schemas import ActionPlan
from app.core.config import settings
from app.db.checkpoint import get_checkpointer
from app.db.session import async_session_factory
from app.models.artifact import Artifact
from app.models.conversation import AgentRun, Conversation

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _QuietPlanner:
    async def ainvoke(self, messages):  # noqa: ANN001
        return ActionPlan(action="chat", response="收到。")


@pytest.fixture(autouse=True)
def _planner():
    set_planner_for_tests(_QuietPlanner())
    yield
    reset_planner()


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))
    return tmp_path


async def _chat(client, thread_id: str, *, with_attachment: bool = True) -> dict:
    payload: dict = {"thread_id": thread_id, "message": "这张表里写了什么？"}

    if with_attachment:
        payload["attachments"] = [
            {
                "data_url": "data:image/png;base64," + base64.b64encode(_PNG).decode(),
                "name": "现场.png",
            }
        ]

    response = await client.post("/api/v1/chat", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    listing = await client.get("/api/v1/conversations?limit=100")
    matched = [c for c in listing.json()["items"] if c["thread_id"] == thread_id]
    assert matched, f"会话 {thread_id} 未出现在列表里"
    return matched[0]


async def _conversation_row(conversation_id: uuid.UUID) -> Conversation:
    async with async_session_factory() as session:
        row = await session.get(Conversation, conversation_id)

    assert row is not None
    return row


async def _count(model, conversation_id: uuid.UUID) -> int:
    async with async_session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.conversation_id == conversation_id)
            )
            or 0
        )


async def test_delete_purges_records_files_and_checkpoints(client, attachment_dir):
    thread_id = f"purge-{uuid.uuid4().hex[:8]}"
    summary = await _chat(client, thread_id)
    conversation_id = uuid.UUID(summary["id"])
    internal_thread = (await _conversation_row(conversation_id)).thread_id

    # 前提：四处都有痕迹，否则这个测试等于什么都没验
    assert list(attachment_dir.rglob("img_*.png")), "附件没有落盘"
    assert await _count(AgentRun, conversation_id) >= 1, "运行记录没写"

    saver = get_checkpointer()
    config = {"configurable": {"thread_id": internal_thread}}
    assert await saver.aget_tuple(config) is not None, "checkpoint 没写"

    response = await client.delete(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 200

    # 1. 业务记录
    listing = await client.get("/api/v1/conversations?limit=100")
    assert all(c["id"] != str(conversation_id) for c in listing.json()["items"])

    # 2. 运行记录：外键只是 SET NULL，必须真的删行，否则原话摘要还在库里
    assert await _count(AgentRun, conversation_id) == 0

    # 3. 图状态：messages / tool_observations / attachments 都在这里面
    assert await saver.aget_tuple(config) is None

    # 4. 磁盘上的原始文件
    assert not list(attachment_dir.rglob("img_*.png"))


async def test_delete_purges_artifact_object_and_row(client, attachment_dir):
    thread_id = f"purge-art-{uuid.uuid4().hex[:8]}"
    summary = await _chat(client, thread_id, with_attachment=False)
    conversation_id = summary["id"]

    # 产物目录是 session 级共享的，用品出前后差集，别去断言「目录空了」
    root = Path(settings.artifact_storage_dir)
    before = {p for p in root.rglob("*") if p.is_file()}

    upload = await client.put(
        f"/api/v1/artifacts?filename=report.txt&conversation_id={conversation_id}",
        content=b"hello-artifact",
        headers={"Content-Type": "text/plain"},
    )
    assert upload.status_code == 200

    created = {p for p in root.rglob("*") if p.is_file()} - before
    assert created, "产物没有落盘"

    response = await client.delete(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 200

    async with async_session_factory() as session:
        remaining = (
            await session.scalars(
                select(Artifact).where(Artifact.conversation_id == conversation_id)
            )
        ).all()
    assert remaining == []

    assert not (created & {p for p in root.rglob("*") if p.is_file()})


async def test_cross_tenant_delete_is_404_and_changes_nothing(client, other_client):
    thread_id = f"purge-cross-{uuid.uuid4().hex[:8]}"
    summary = await _chat(client, thread_id, with_attachment=False)
    conversation_id = uuid.UUID(summary["id"])

    response = await other_client.delete(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 404

    # 404 不能是「删了一半才发现越权」
    listing = await client.get("/api/v1/conversations?limit=100")
    assert any(c["id"] == str(conversation_id) for c in listing.json()["items"])

    detail = await client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert await _count(AgentRun, conversation_id) >= 1
