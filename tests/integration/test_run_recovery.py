# tests/integration/test_run_recovery.py
"""请求总时限与遗留运行记录的收尾。

两个都是 2026-11-04 那次「客户端早已 504、后端又空转 45 分钟」的直接产物：

- nginx 断开客户端 ≠ 取消后端协程，必须由服务端自己设总时限；
- 进程重启会留下永远 RUNNING 的运行记录，监控据此以为还有请求在跑。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.conversation import (
    AgentRun,
    Conversation,
    Message,
    MessageRole,
    RunStatus,
)
from app.tenancy.context import tenant_scope


async def test_chat_timeout_returns_error_and_closes_run(client, monkeypatch):
    from app.services import chat_service

    class _HangingGraph:
        async def ainvoke(self, *_args, **_kwargs):
            await asyncio.sleep(30)

    monkeypatch.setattr(chat_service, "get_agent_graph", lambda: _HangingGraph())
    monkeypatch.setattr(chat_service.settings, "chat_timeout_seconds", 1)

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-timeout", "message": "查一下这个站点的压力情况"},
    )

    body = response.json()
    assert body["status"] == "error"
    assert body["error_category"] == "chat_timeout"
    assert "已中止" in body["answer"]

    # 超时不能只回个错：会话里要留下痕迹，run 也要收尾，
    # 否则用户刷新后只看到自己的提问，而运行记录永远是 RUNNING。
    with tenant_scope("default"):
        async with async_session_factory() as session:
            conversation = (
                await session.execute(
                    select(Conversation).where(
                        Conversation.thread_id.like("%:thread-timeout")
                    )
                )
            ).scalar_one()
            messages = (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at)
                )
            ).scalars().all()
            assert messages[-1].role is MessageRole.ASSISTANT
            assert "已中止" in messages[-1].content

            run = await session.get(AgentRun, uuid.UUID(body["run_id"]))
            assert run is not None
            assert run.status is RunStatus.FAILED
            assert run.error_category == "chat_timeout"
            assert run.finished_at is not None


async def test_stale_running_runs_are_marked_failed():
    from app.services.run_service import interrupt_stale_runs

    thread_id = f"thread-stale-{uuid.uuid4().hex[:8]}"

    with tenant_scope("default"):
        async with async_session_factory() as session:
            async with session.begin():
                conversation = Conversation(
                    tenant_id="default",
                    user_id="test-user",
                    thread_id=thread_id,
                    title="残留运行记录",
                )
                session.add(conversation)
                await session.flush()
                run = AgentRun(
                    conversation_id=conversation.id,
                    tenant_id="default",
                    user_id="test-user",
                    thread_id=thread_id,
                    status=RunStatus.RUNNING,
                    input_summary="进程中断时正在执行的请求",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

    interrupted = await interrupt_stale_runs()
    assert interrupted >= 1

    with tenant_scope("default"):
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run.status is RunStatus.FAILED
            assert run.error_category == "process_restarted"
            assert run.finished_at is not None
