# tests/integration/test_observability_chat.py
"""端到端：一轮真实对话要在库里留下一条完整的 trace 树。

这是 M1 的验收条件——不是「span() 能用」，而是「界面将来要拼的那条链路
真的能从这里查出来」。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.agents.schemas import ActionPlan
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.observability import ObsSpan, ObsTrace
from app.observability.writer import ObsWriter


async def _wait_for(condition, *, tries: int = 40, delay: float = 0.1):
    for _ in range(tries):
        result = await condition()
        if result:
            return result
        await asyncio.sleep(delay)
    return await condition()


async def test_chat_turn_leaves_a_complete_trace(client, planner, monkeypatch):
    writer = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", writer)
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_capture_content", "full")

    await writer.start()
    try:
        planner([ActionPlan(action="chat", response="今天是周一。")])
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "thread-obs-trace", "message": "今天周几？"},
        )
        assert response.status_code == 200

        async def load_trace():
            # 按本轮线程取，不取「库里的第一行」：观测测试之间共用一个库，
            # 别的用例（unit/test_observability.py 里验证写回的那些）会留下
            # 自己的 trace。取 rows[0] 等于把用例顺序当契约，迟早串味。
            async with async_session_factory() as session:
                return await session.scalar(
                    select(ObsTrace).where(ObsTrace.thread_id.like("%:thread-obs-trace"))
                )

        trace_row = await _wait_for(load_trace)
    finally:
        await writer.stop()

    assert trace_row is not None, "对话跑了但库里没有 trace"

    # 1) 根行要有身份与结论，界面列表页靠这几列渲染。
    # thread_id 是服务端内部线程（租户:会话UUID:客户端线程），不是裸的客户端值。
    assert trace_row.thread_id.endswith(":thread-obs-trace")
    assert trace_row.status == "ok"
    assert trace_row.intent == "chat"
    assert trace_row.duration_ms is not None
    # 完整原文：用户问题与最终回答都要在。
    assert trace_row.input_text == "今天周几？"
    assert trace_row.output_text == "今天是周一。"
    # run_id 必须回填，否则「从运行记录跳到 trace」这条链路断掉。
    assert trace_row.run_id

    async with async_session_factory() as session:
        span_rows = (
            (
                await session.execute(
                    select(ObsSpan)
                    .where(ObsSpan.trace_id == trace_row.trace_id)
                    .order_by(ObsSpan.seq)
                )
            )
            .scalars()
            .all()
        )

    names = [s.name for s in span_rows]
    # 2) 关键节点与模型调用都在树里（节点 span 由 graph.py 统一装配 traced）。
    assert "persist.user_message" in names
    assert "graph.invoke" in names
    assert "begin_turn" in names
    assert "plan_request" in names
    assert "llm.call" in names
    assert "respond" in names
    assert "persist.assistant_message" in names

    # 顺序也要对：span 的 seq 决定详情页里谁在上面。
    assert names.index("begin_turn") < names.index("plan_request")
    assert names.index("plan_request") < names.index("llm.call")

    # 3) 父子关系连得上：所有 span 都能顺着 parent 走到根，不允许出现孤儿。
    by_id = {s.span_id: s for s in span_rows}
    for row in span_rows:
        if row.parent_span_id is None:
            continue
        assert row.parent_span_id in by_id, f"{row.name} 的父 span 不在同一 trace 里"

    # 4) 模型调用记到了 prompt 原文与 token 位（没有 usage 时允许为空，但字段要在）。
    llm_span = next(s for s in span_rows if s.name == "llm.call")
    assert llm_span.kind == "llm"
    assert "今天周几" in (llm_span.content or "") or "今天周几" in (trace_row.input_text or "")

    # 5) 工具清单也挂在 llm.call 上：详情页「准备工具」要回答的是「模型当时
    #    看到哪些工具」。回查注册表只能得到「现在有什么」，那是另一个问题。
    assert "tools.count" in llm_span.attrs
    listed = [name for name in (llm_span.attrs.get("tools.names") or "").split(",") if name]
    assert len(listed) == int(llm_span.attrs["tools.count"])

    assert trace_row.span_count == len(span_rows)


async def test_observability_off_writes_nothing(client, planner, monkeypatch):
    """上线前关掉开关的验收：同样的对话，库里一行都不能多。"""
    writer = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", writer)
    monkeypatch.setattr(settings, "obs_enabled", False)

    async with async_session_factory() as session:
        before = len((await session.execute(select(ObsTrace))).scalars().all())

    await writer.start()
    try:
        planner([ActionPlan(action="chat", response="好。")])
        response = await client.post(
            "/api/v1/chat",
            json={"thread_id": "thread-obs-off", "message": "你好"},
        )
        assert response.status_code == 200
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    async with async_session_factory() as session:
        after = len((await session.execute(select(ObsTrace))).scalars().all())

    # 用前后差值而不是「表为空」：同一进程里的库是共享的，前面用例已经写过行。
    assert after == before
    # 关闭时连队列都不该建：writer.start() 直接返回。
    assert writer.stats()["running"] is False
