# tests/integration/test_knowledge_recent_query.py
"""验证页要能拿回「对话里真正送进检索的那句话」。

走完整一轮对话（规划器 → knowledge 节点 → 落库），再从接口读回来。
这条链路的意义：用户看到的原话与真正检索的句子不是同一句，
而验证页只有拿到后者，才能复现对话里的命中结果。
"""

from __future__ import annotations

from app.agents.schemas import ActionPlan

PLANNED = "本安设备（本质安全型设备）要求 防爆电气设备 通用要求"
ASKED = "本安设备有些要求？"


async def test_recent_query_is_the_planned_one_not_the_user_text(client, planner):
    planner([ActionPlan(action="knowledge", query=PLANNED)])

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-recent-query", "message": ASKED},
    )
    assert response.status_code == 200
    # 这条用例的库里没有可命中的切片，答复走的是「资料不足」那一路；
    # 这里只关心改写发生在检索入口、并被原样记录下来。
    assert response.json()["metadata"]["knowledge_query"] == PLANNED

    recent = await client.get("/api/v1/knowledge/recent-query")

    assert recent.status_code == 200
    body = recent.json()
    assert body["query"] == PLANNED
    assert body["query"] != ASKED
    assert body["conversation_id"]


async def test_recent_query_survives_a_refresh(client, planner):
    """刷新会话后再开验证页，那一句仍要读得到（不依赖内存里的上一轮状态）。"""
    planner([ActionPlan(action="knowledge", query="调压箱 关闭压力 试验")])

    await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-recent-query-reload", "message": "关闭压力试验做几次"},
    )

    second = await client.get("/api/v1/knowledge/recent-query")

    assert second.status_code == 200
    assert second.json()["query"] == "调压箱 关闭压力 试验"


def _chunk() -> dict:
    return {
        "chunk_id": "cut-0",
        "document_id": "cut",
        "chunk_index": 0,
        "text": "系统正常工作时处于开启状态，发生事故时自动切断燃气，故障排除后需人工复位。",
        "title": "GB/T 41315-2022 城镇燃气输配系统用安全切断阀",
        "source_path": "notes/cut.md",
        "section": "3.1 术语和定义",
        "updated_at": "2026-11-18T00:00:00+00:00",
        "content_hash": "sha256:" + "b" * 64,
        "access_scope": "internal",
    }


async def test_answer_mode_is_persisted_with_the_message(client, planner, monkeypatch):
    """归纳模式和回落原因必须跟着消息落库。

    这条钉的是一个真实漏网的接缝：节点把 ``knowledge_answer_mode`` 放进了
    ``response_metadata``，但 ``chat_service`` 落库时只按白名单抄了
    ``knowledge_query``，于是接口响应里有、库里没有——刷新之后
    「这条答复是归纳的还是摘的」就再也查不到了。
    """
    from sqlalchemy import select

    from app.knowledge import compose as compose_module
    from app.knowledge import rerank as rerank_module
    from app.knowledge.retriever import KnowledgeRetriever
    from app.models.conversation import Message, MessageRole

    retriever = KnowledgeRetriever([_chunk()])

    async def fake_retriever(tenant_id):
        return retriever

    async def fake_rerank_model(prompt: str, *, timeout: float) -> str:
        return '{"keep": [1]}'

    async def fake_compose_model(prompt: str, *, timeout: float) -> str:
        return "切断阀在事故或故障时自动、完全切断燃气，故障排除后需人工复位 [1]。"

    monkeypatch.setattr("app.knowledge.service._retriever_for", fake_retriever)
    monkeypatch.setattr(rerank_module, "_ask_model", fake_rerank_model)
    monkeypatch.setattr(compose_module, "_ask_model", fake_compose_model)

    planner([ActionPlan(action="knowledge", query="切断阀有何作用？")])

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-answer-mode", "message": "切断阀有何作用？"},
    )

    body = response.json()
    assert body["metadata"]["knowledge_answer_mode"] == "summary"
    assert "以上为基于上述 1 处资料的归纳" in body["answer"]

    from app.db.session import async_session_factory

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(Message)
                .where(Message.role == MessageRole.ASSISTANT)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one()

    assert row.metadata_json["knowledge_answer_mode"] == "summary"
